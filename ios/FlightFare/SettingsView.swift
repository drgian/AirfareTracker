import SwiftUI
import UserNotifications

struct SettingsView: View {
    @EnvironmentObject private var session: Session
    @Environment(\.dismiss) private var dismiss

    @State private var notifications: UNAuthorizationStatus = .notDetermined
    @State private var confirmingDelete = false
    @State private var problem: String?
    @State private var busy = false

    var body: some View {
        NavigationStack {
            List {
                Section("Signed in as") {
                    Text(session.email ?? "—").foregroundStyle(.secondary)
                }

                Section {
                    HStack {
                        Text("Notifications")
                        Spacer()
                        Text(notificationsLabel).foregroundStyle(.secondary)
                    }
                    if notifications == .denied {
                        Button("Open Settings") {
                            if let url = URL(string: UIApplication.openSettingsURLString) {
                                UIApplication.shared.open(url)
                            }
                        }
                    }
                } footer: {
                    Text("Price alerts and the daily summary arrive here as well as by email.")
                }

                Section {
                    Button("Sign out") { signOut() }
                    Button("Delete account", role: .destructive) { confirmingDelete = true }
                }

                if let problem {
                    Section { Text(problem).font(.footnote).foregroundStyle(.red) }
                }

                Section {
                    HStack {
                        Text("Version").foregroundStyle(.secondary)
                        Spacer()
                        Text(Env.appVersion).foregroundStyle(.secondary)
                    }
                    .font(.footnote)
                }
            }
            .navigationTitle("Account")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } }
            }
            .disabled(busy)
            .task {
                notifications = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
            }
            .alert("Delete your account?", isPresented: $confirmingDelete) {
                Button("Cancel", role: .cancel) {}
                Button("Delete", role: .destructive) { deleteAccount() }
            } message: {
                Text("Your account and every trip you're tracking are removed for good. This can't be undone.")
            }
        }
    }

    private var notificationsLabel: String {
        switch notifications {
        case .authorized, .provisional, .ephemeral: return "On"
        case .denied: return "Off — turn on in Settings"
        default: return "Not asked yet"
        }
    }

    /// Stop pushing to this phone before the session goes away, or we'd keep notifying
    /// a device nobody is signed in on.
    private func signOut() {
        busy = true
        Task {
            if let token = AppDelegate.deviceToken { try? await API.shared.forgetDevice(token: token) }
            try? await API.shared.signOut()
            await MainActor.run { session.end(); busy = false; dismiss() }
        }
    }

    private func deleteAccount() {
        busy = true
        Task {
            do {
                try await API.shared.deleteAccount()
                await MainActor.run { session.end(); busy = false; dismiss() }
            } catch {
                await MainActor.run { problem = error.localizedDescription; busy = false }
            }
        }
    }
}
