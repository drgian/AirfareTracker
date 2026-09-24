import Foundation
import Security

/// Where the sign-in token lives. The Keychain rather than UserDefaults, because it survives
/// reinstalls of the app only if we want it to, and isn't readable from a backup of the device.
final class Session: ObservableObject {
    static let shared = Session()

    private let account = "session-token"
    private let service = "io.flightfare.app"

    @Published private(set) var token: String?
    @Published var email: String?
    @Published var role: String?

    private init() {
        token = readKeychain()
        email = UserDefaults.standard.string(forKey: "email")
        role = UserDefaults.standard.string(forKey: "role")
    }

    var isSignedIn: Bool { token != nil }

    func begin(token: String) {
        writeKeychain(token)
        self.token = token
    }

    /// Forget everything about who was signed in. Safe to call twice.
    func end() {
        deleteKeychain()
        token = nil
        email = nil
        role = nil
        UserDefaults.standard.removeObject(forKey: "email")
        UserDefaults.standard.removeObject(forKey: "role")
    }

    func remember(_ me: Me) {
        email = me.email
        role = me.role
        UserDefaults.standard.set(me.email, forKey: "email")
        UserDefaults.standard.set(me.role, forKey: "role")
    }

    /// Research is for researchers and admins; everyone else never sees the tab.
    var canResearch: Bool { role == "admin" || role == "researcher" }

    // MARK: Keychain

    private func query() -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    private func readKeychain() -> String? {
        var q = query()
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private func writeKeychain(_ value: String) {
        deleteKeychain()
        var q = query()
        q[kSecValueData as String] = Data(value.utf8)
        // Available once the phone has been unlocked since boot; notifications can still arrive
        // before that, but we only need the token when someone is actually using the app.
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(q as CFDictionary, nil)
    }

    private func deleteKeychain() {
        SecItemDelete(query() as CFDictionary)
    }
}
