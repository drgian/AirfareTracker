import SwiftUI
import UserNotifications

@main
struct FlightFareApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var session = Session.shared

    var body: some Scene {
        WindowGroup {
            Group {
                if session.isSignedIn {
                    RootTabs()
                } else {
                    SignInView()
                }
            }
            .environmentObject(session)
            .tint(.accentColor)
            .preferredColorScheme(.dark)
        }
    }
}

/// Trips, and - for researchers and admins - the routes being studied. Admin itself
/// deliberately stays on the website: an app that shows other people's accounts invites
/// questions at review time, and it isn't something you need on a phone.
struct RootTabs: View {
    @EnvironmentObject private var session: Session

    var body: some View {
        TabView {
            TripsView()
                .tabItem { Label("Trips", systemImage: "airplane") }
            if session.canResearch {
                ResearchView()
                    .tabItem { Label("Research", systemImage: "chart.xyaxis.line") }
            }
        }
        // The role may have changed since last launch, and it decides whether the tab is there
        .task {
            if let me = try? await API.shared.me() { session.remember(me) }
        }
    }
}

/// Everything to do with notifications: asking permission, handing Apple's device token
/// to our API, and deciding what a notification does when it arrives.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    static private(set) var deviceToken: String?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        // Only ask once someone is signed in — an alert about prices means nothing before that.
        if Session.shared.isSignedIn { Task { await Self.askAndRegister() } }
        return true
    }

    /// Ask for permission if we haven't, then register with Apple. Quietly does nothing if refused.
    static func askAndRegister() async {
        let centre = UNUserNotificationCenter.current()
        let settings = await centre.notificationSettings()
        if settings.authorizationStatus == .notDetermined {
            guard let granted = try? await centre.requestAuthorization(options: [.alert, .sound, .badge]),
                  granted else { return }
        } else if settings.authorizationStatus == .denied {
            return
        }
        await MainActor.run { UIApplication.shared.registerForRemoteNotifications() }
    }

    func application(_ application: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
        let hex = token.map { String(format: "%02x", $0) }.joined()
        Self.deviceToken = hex
        Task {
            do { try await API.shared.registerDevice(token: hex) }
            catch { print("couldn't register for notifications: \(error.localizedDescription)") }
        }
    }

    func application(_ application: UIApplication,
                     didFailToRegisterForRemoteNotificationsWithError error: Error) {
        print("Apple wouldn't give us a device token: \(error.localizedDescription)")
    }

    /// Show the alert even when the app is already open — a price drop is worth interrupting for.
    func userNotificationCenter(_ centre: UNUserNotificationCenter,
                                willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }

    /// Tapping a notification should land on fresh prices, not whatever was on screen last.
    func userNotificationCenter(_ centre: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse) async {
        await MainActor.run { NotificationCenter.default.post(name: .refreshTrips, object: nil) }
    }
}

extension Notification.Name {
    static let refreshTrips = Notification.Name("refreshTrips")
}
