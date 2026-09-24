import SwiftUI
import AuthenticationServices

struct SignInView: View {
    @EnvironmentObject private var session: Session
    @State private var problem: String?
    @State private var working = false

    var body: some View {
        VStack(spacing: 0) {
            Spacer()
            Image(systemName: "airplane.departure")
                .font(.system(size: 52, weight: .light))
                .foregroundStyle(.tint)
            Text("FlightFare")
                .font(.largeTitle.weight(.semibold))
                .padding(.top, 16)
            Text("We watch the airline's own price, every day,\nand tell you when it moves.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.top, 8)
            Spacer()

            SignInWithAppleButton(.signIn) { request in
                request.requestedScopes = [.email]
            } onCompletion: { result in
                handle(result)
            }
            .signInWithAppleButtonStyle(.white)   // the app runs dark; a black button would vanish
            .frame(height: 50)
            .disabled(working)
            .opacity(working ? 0.5 : 1)

            if let problem {
                Text(problem)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .padding(.top, 12)
            }

            Text("Choose \u{201C}Share My Email\u{201D} so your alerts reach you\nand you land on the account you already have.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.top, 20)
            Spacer().frame(height: 24)
        }
        .padding(.horizontal, 32)
    }

    private func handle(_ result: Result<ASAuthorization, Error>) {
        problem = nil
        switch result {
        case .failure(let error):
            // Backing out of the sheet isn't a failure worth shouting about.
            if (error as? ASAuthorizationError)?.code != .canceled {
                problem = "Apple sign-in didn't finish. Please try again."
            }
        case .success(let auth):
            guard let credential = auth.credential as? ASAuthorizationAppleIDCredential,
                  let data = credential.identityToken,
                  let identityToken = String(data: data, encoding: .utf8) else {
                problem = "Apple didn't send us anything we could use. Please try again."
                return
            }
            working = true
            Task {
                do {
                    let token = try await API.shared.signInWithApple(identityToken: identityToken)
                    await MainActor.run { session.begin(token: token) }
                    if let me = try? await API.shared.me() {
                        await MainActor.run { session.remember(email: me.email) }
                    }
                    await AppDelegate.askAndRegister()
                } catch {
                    await MainActor.run { problem = error.localizedDescription }
                }
                await MainActor.run { working = false }
            }
        }
    }
}
