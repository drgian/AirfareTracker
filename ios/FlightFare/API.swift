import Foundation

// Debug builds talk to the dev API, which is the one that gets new code first.
// Release builds talk to the live one. Nothing else in the app knows the difference.
enum Env {
    #if DEBUG
    static let apiBase = URL(string: "https://api.flightfare.io/dev")!
    static let apnsEnvironment = "sandbox"
    #else
    static let apiBase = URL(string: "https://api.flightfare.io")!
    static let apnsEnvironment = "production"
    #endif

    static var appVersion: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?"
        return "\(v) (\(b))"
    }
}

/// An error we can actually show someone. The API sends its own wording in `detail`,
/// which is already written for people, so we pass it through untouched.
struct APIError: LocalizedError {
    let status: Int
    let message: String
    var errorDescription: String? { message }
    var isUnauthorized: Bool { status == 401 }
}

// MARK: - What the API sends back

struct PricePoint: Codable, Identifiable, Hashable {
    let scrapedAt: Date
    let price: Double
    let stops: Int?
    let departTime: String?
    let arriveTime: String?
    let flights: String?

    var id: Date { scrapedAt }

    enum CodingKeys: String, CodingKey {
        case scrapedAt = "scraped_at"
        case price, stops, flights
        case departTime = "depart_time"
        case arriveTime = "arrive_time"
    }
}

struct Trip: Codable, Identifiable, Hashable {
    let id: Int
    let origin: String
    let destination: String
    let travelDate: String
    let returnDate: String?
    let airline: String?
    let cabinClass: String?
    let label: String?
    let alertBelow: Int?
    let archived: Bool
    let status: String?
    let statusDetail: String?
    let checkPending: Bool?
    let history: [PricePoint]

    enum CodingKeys: String, CodingKey {
        case id, origin, destination, airline, label, archived, status, history
        case travelDate = "travel_date"
        case returnDate = "return_date"
        case cabinClass = "cabin_class"
        case alertBelow = "alert_below"
        case statusDetail = "status_detail"
        case checkPending = "check_pending"
    }

    var name: String {
        if let label, !label.isEmpty { return label }
        return "\(origin) → \(destination)"
    }

    var latest: PricePoint? { history.last }

    /// How the newest price compares with the one before it — what the list shows at a glance.
    var change: Double? {
        guard history.count >= 2 else { return nil }
        return history[history.count - 1].price - history[history.count - 2].price
    }

    var lowest: PricePoint? { history.min(by: { $0.price < $1.price }) }

    var isBelowTarget: Bool {
        guard let alertBelow, let latest else { return false }
        return latest.price < Double(alertBelow)
    }
}

struct Cabin: Codable, Hashable, Identifiable {
    let value: String
    let label: String
    var id: String { value }
}

struct Airline: Codable, Hashable, Identifiable {
    let code: String
    let name: String
    let defaultCabin: String
    let cabins: [Cabin]

    var id: String { code }

    enum CodingKeys: String, CodingKey {
        case code, name, cabins
        case defaultCabin = "default_cabin"
    }
}

/// What the Add trip form is holding before it's good enough to send.
struct TripDraft {
    var origin = ""
    var destination = ""
    var travelDate = Calendar.current.date(byAdding: .day, value: 30, to: .now) ?? .now
    var returnDate = Calendar.current.date(byAdding: .day, value: 37, to: .now) ?? .now
    var airline = "DL"
    var cabin = "main_classic"
    var flights = ""
    var label = ""
    var alertBelow: Int?

    /// Why the form can't be submitted yet, in the same words the API would use.
    var problem: String? {
        if origin.isEmpty || destination.isEmpty { return "Pick both airports from the list." }
        if origin == destination { return "The origin and destination must be different airports." }
        if travelDate.startOfDay <= Date.now.startOfDay { return "The departure date must be in the future." }
        if returnDate.startOfDay <= travelDate.startOfDay { return "The return date must be after the departure date." }
        return nil
    }
}

extension Date {
    var apiDate: String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.calendar = Calendar(identifier: .gregorian)
        return f.string(from: self)
    }

    var startOfDay: Date { Calendar.current.startOfDay(for: self) }
}

struct Me: Codable {
    let email: String
    let role: String?
    let isAdmin: Bool?

    enum CodingKeys: String, CodingKey {
        case email, role
        case isAdmin = "is_admin"
    }
}

// MARK: - The client

actor API {
    static let shared = API()

    private let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 20
        c.waitsForConnectivity = true
        return c
    }()

    private lazy var decoder: JSONDecoder = {
        let d = JSONDecoder()
        // The API sends naive UTC timestamps, with and without fractional seconds.
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        d.dateDecodingStrategy = .custom { decoder in
            var text = try decoder.singleValueContainer().decode(String.self)
            if !text.hasSuffix("Z") && !text.contains("+") { text += "Z" }
            if let date = withFraction.date(from: text) ?? plain.date(from: text) { return date }
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "Not a date we recognise: \(text)"))
        }
        return d
    }()

    private func request(_ method: String, _ path: String, body: [String: Any?]? = nil) throws -> URLRequest {
        var r = URLRequest(url: Env.apiBase.appendingPathComponent(path))
        r.httpMethod = method
        if let token = Session.shared.token {
            r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try JSONSerialization.data(withJSONObject: body.compactMapValues { $0 })
        }
        return r
    }

    private func send<T: Decodable>(_ r: URLRequest, as type: T.Type) async throws -> T {
        let (data, response) = try await session.data(for: r)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw APIError(status: status, message: detail ?? "Something went wrong (\(status)). Please try again.")
        }
        if T.self == Empty.self { return Empty() as! T }
        return try decoder.decode(T.self, from: data)
    }

    struct Empty: Decodable {}

    // MARK: Calls the app makes

    func signInWithApple(identityToken: String) async throws -> String {
        struct Out: Decodable { let session: String }
        let r = try request("POST", "auth/apple/native", body: ["identity_token": identityToken])
        return try await send(r, as: Out.self).session
    }

    func me() async throws -> Me {
        try await send(try request("GET", "me"), as: Me.self)
    }

    func trips() async throws -> [Trip] {
        try await send(try request("GET", "trips"), as: [Trip].self)
    }

    func setAlert(tripID: Int, below: Int?) async throws -> Trip {
        let r = try request("PATCH", "trips/\(tripID)", body: ["alert_below": below ?? 0])
        return try await send(r, as: Trip.self)
    }

    func rename(tripID: Int, to label: String) async throws -> Trip {
        try await send(try request("PATCH", "trips/\(tripID)", body: ["label": label]), as: Trip.self)
    }

    func airlines() async throws -> [Airline] {
        struct Out: Decodable { let airlines: [Airline] }
        return try await send(try request("GET", "airlines"), as: Out.self).airlines
    }

    func addTrip(_ draft: TripDraft) async throws -> Trip {
        let r = try request("POST", "trips", body: [
            "origin": draft.origin,
            "destination": draft.destination,
            "travel_date": draft.travelDate.apiDate,
            "return_date": draft.returnDate.apiDate,
            "airline": draft.airline,
            "cabin_class": draft.cabin,
            "outbound_flights": draft.flights.trimmingCharacters(in: .whitespaces),
            "label": draft.label.trimmingCharacters(in: .whitespaces),
            "alert_below": draft.alertBelow,
        ])
        return try await send(r, as: Trip.self)
    }

    func archive(tripID: Int) async throws -> Trip {
        try await send(try request("POST", "trips/\(tripID)/archive"), as: Trip.self)
    }

    func restore(tripID: Int) async throws -> Trip {
        try await send(try request("POST", "trips/\(tripID)/restore"), as: Trip.self)
    }

    func deleteTrip(id: Int) async throws {
        _ = try await send(try request("DELETE", "trips/\(id)"), as: Empty.self)
    }

    func registerDevice(token: String) async throws {
        let r = try request("POST", "me/devices", body: [
            "token": token, "platform": "ios",
            "environment": Env.apnsEnvironment, "app_version": Env.appVersion,
        ])
        _ = try await send(r, as: Empty.self)
    }

    func forgetDevice(token: String) async throws {
        _ = try await send(try request("DELETE", "me/devices/\(token)"), as: Empty.self)
    }

    func signOut() async throws {
        _ = try await send(try request("POST", "auth/logout"), as: Empty.self)
    }

    func deleteAccount() async throws {
        _ = try await send(try request("DELETE", "me"), as: Empty.self)
    }
}
