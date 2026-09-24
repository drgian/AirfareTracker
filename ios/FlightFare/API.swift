import Foundation

// Both builds talk to the live API. Dev would be the safer choice, but nothing checks
// prices there - its newest price is from 16 September - so a trip added against dev sits
// at "Checking..." for ever and the app looks broken. Point this back at /dev the day
// something services that queue.
//
// Notifications still follow the build, and must: a debug build's device token is only
// valid against Apple's sandbox gateway, a release build's only against production.
enum Env {
    static let apiBase = URL(string: "https://api.flightfare.io")!

    #if DEBUG
    static let apnsEnvironment = "sandbox"
    #else
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

extension KeyedDecodingContainer {
    /// Some of these columns are text in the database and arrive quoted ("stops":"1"),
    /// others arrive as numbers. Take either rather than failing the whole response.
    func flexibleInt(_ key: Key) -> Int? {
        if let n = try? decodeIfPresent(Int.self, forKey: key) { return n }
        if let d = try? decodeIfPresent(Double.self, forKey: key) { return Int(d) }
        if let s = try? decodeIfPresent(String.self, forKey: key) { return Int(s.trimmingCharacters(in: .whitespaces)) }
        return nil
    }

    func flexibleDouble(_ key: Key) -> Double? {
        if let d = try? decodeIfPresent(Double.self, forKey: key) { return d }
        if let s = try? decodeIfPresent(String.self, forKey: key) { return Double(s.trimmingCharacters(in: .whitespaces)) }
        return nil
    }
}

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

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        scrapedAt  = try c.decode(Date.self, forKey: .scrapedAt)
        price      = c.flexibleDouble(.price) ?? 0
        stops      = c.flexibleInt(.stops)
        departTime = try c.decodeIfPresent(String.self, forKey: .departTime)
        arriveTime = try c.decodeIfPresent(String.self, forKey: .arriveTime)
        flights    = try c.decodeIfPresent(String.self, forKey: .flights)
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

/// A route we price every week out to six months, to learn how booking early pays off.
struct ResearchRoute: Codable, Identifiable, Hashable {
    let id: Int
    let label: String
    let origin: String
    let destination: String
    let airline: String
    let nights: Int
    let weekday: Int
    let weeks: Int
    let observations: Int
    let priced: Int
    let lowest: Double?
    let highest: Double?
    let lastDay: String?
    let waiting: Int

    enum CodingKeys: String, CodingKey {
        case id, label, origin, destination, airline, nights, weekday, weeks
        case observations, priced, lowest, highest, waiting
        case lastDay = "last_day"
    }

    static let weekdayNames = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    var departsOn: String { Self.weekdayNames[min(max(weekday, 0), 6)] }
    var summary: String { "\(departsOn)s · \(nights) nights · \(weeks) weeks out" }
}

/// One observation: what this trip cost on one day, booked this far ahead.
struct ResearchPoint: Codable, Identifiable, Hashable {
    let observedOn: String
    let departureDate: String
    let daysOut: Int
    let price: Double?
    let status: String?
    let oil: Double?
    let flights: String?

    var id: String { observedOn + departureDate }

    enum CodingKeys: String, CodingKey {
        case status, flights
        case observedOn = "observed_on"
        case departureDate = "departure_date"
        case daysOut = "days_out"
        case price = "price_usd"
        case oil = "oil_usd"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        observedOn    = try c.decode(String.self, forKey: .observedOn)
        departureDate = try c.decode(String.self, forKey: .departureDate)
        daysOut       = c.flexibleInt(.daysOut) ?? 0
        price         = c.flexibleDouble(.price)
        oil           = c.flexibleDouble(.oil)
        status        = try c.decodeIfPresent(String.self, forKey: .status)
        flights       = try c.decodeIfPresent(String.self, forKey: .flights)
    }
}

struct ResearchData: Codable {
    let route: ResearchRoute
    let prices: [ResearchPoint]
}

// MARK: - Analysis: patterns across every price we have ever recorded

/// A slice of the data - one weekday, one time of day, one booking window - and how its
/// prices compare with the average for the same trip. `relative` is a fraction: -0.03 means
/// three percent below that route's own average, which is the only fair way to compare a
/// $300 hop with a $1,500 long haul.
struct AnalysisSlice: Codable, Identifiable, Hashable {
    let label: String
    let relative: Double?
    let spread: Double?
    let count: Int

    var id: String { label }

    /// Too little data to mean anything. Drawn faintly rather than hidden, so the gap shows.
    var thin: Bool { count < 10 }
}

struct AnalysisChanges: Codable, Hashable {
    let pairs: Int
    let changed: Int
    let ups: Int
    let downs: Int
    let averageMove: Double?
    let averageMovePercent: Double?
    let biggestDrop: Double?
    let biggestRise: Double?

    enum CodingKeys: String, CodingKey {
        case pairs, changed, ups, downs
        case averageMove = "avg_move"
        case averageMovePercent = "avg_move_pct"
        case biggestDrop = "biggest_drop"
        case biggestRise = "biggest_rise"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        pairs   = c.flexibleInt(.pairs) ?? 0
        changed = c.flexibleInt(.changed) ?? 0
        ups     = c.flexibleInt(.ups) ?? 0
        downs   = c.flexibleInt(.downs) ?? 0
        averageMove        = c.flexibleDouble(.averageMove)
        averageMovePercent = c.flexibleDouble(.averageMovePercent)
        biggestDrop        = c.flexibleDouble(.biggestDrop)
        biggestRise        = c.flexibleDouble(.biggestRise)
    }

    var steadyShare: Double { pairs == 0 ? 0 : Double(pairs - changed) / Double(pairs) }
}

struct Analysis: Decodable {
    let checks: Int
    let routes: Int
    let usable: Int
    let since: String?
    let byWeekday: [AnalysisSlice]
    let byTimeOfDay: [AnalysisSlice]
    let byDaysAhead: [AnalysisSlice]
    let changes: AnalysisChanges

    private static let weekdays = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    enum Top: String, CodingKey { case summary, by_dow, by_time, by_days_out, changes }
    enum Summary: String, CodingKey { case checks, routes, usable, since }
    enum Slice: String, CodingKey { case dow, slot, label, rel, sd, n }

    init(from decoder: Decoder) throws {
        let top = try decoder.container(keyedBy: Top.self)
        let s = try top.nestedContainer(keyedBy: Summary.self, forKey: .summary)
        checks = s.flexibleInt(.checks) ?? 0
        routes = s.flexibleInt(.routes) ?? 0
        usable = s.flexibleInt(.usable) ?? 0
        since  = try s.decodeIfPresent(String.self, forKey: .since)
        changes = try top.decode(AnalysisChanges.self, forKey: .changes)

        // The three groupings name their bucket differently: a weekday number, a slot name,
        // or a ready-made label. Read whichever is there.
        func slices(_ key: Top) throws -> [AnalysisSlice] {
            var list = try top.nestedUnkeyedContainer(forKey: key)
            var out: [AnalysisSlice] = []
            while !list.isAtEnd {
                let c = try list.nestedContainer(keyedBy: Slice.self)
                let name: String
                if let dow = c.flexibleInt(.dow) {
                    name = Self.weekdays[min(max(dow, 1), 7)]
                } else if let slot = try c.decodeIfPresent(String.self, forKey: .slot) {
                    name = slot
                } else {
                    name = (try c.decodeIfPresent(String.self, forKey: .label)) ?? "?"
                }
                out.append(AnalysisSlice(label: name, relative: c.flexibleDouble(.rel),
                                         spread: c.flexibleDouble(.sd), count: c.flexibleInt(.n) ?? 0))
            }
            return out
        }
        byWeekday   = try slices(.by_dow)
        byTimeOfDay = try slices(.by_time)
        byDaysAhead = try slices(.by_days_out)
    }
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

// The API sends naive UTC timestamps, sometimes with fractional seconds and sometimes without.
// Formatters are expensive to build and these are only ever read, so they live here and are shared.
private nonisolated(unsafe) let isoWithFraction: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()

private nonisolated(unsafe) let isoPlain: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

// MARK: - The client

actor API {
    static let shared = API()

    private let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 20
        c.waitsForConnectivity = true
        return URLSession(configuration: c)
    }()

    private lazy var decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { decoder in
            var text = try decoder.singleValueContainer().decode(String.self)
            if !text.hasSuffix("Z") && !text.contains("+") { text += "Z" }
            if let date = isoWithFraction.date(from: text) ?? isoPlain.date(from: text) { return date }
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "Not a date we recognise: \(text)"))
        }
        return d
    }()

    private func request(_ method: String, _ path: String,
                         query: [String: String] = [:],
                         body: [String: Any?]? = nil) throws -> URLRequest {
        var parts = URLComponents(url: Env.apiBase.appendingPathComponent(path),
                                  resolvingAgainstBaseURL: false)
        if !query.isEmpty {
            parts?.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        guard let url = parts?.url else {
            throw APIError(status: 0, message: "Couldn't build a request for \(path).")
        }
        var r = URLRequest(url: url)
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
        do {
            return try decoder.decode(T.self, from: data)
        } catch let error as DecodingError {
            // Say which field, not just "isn't in the correct format" — that message costs
            // an hour every time the API grows a column.
            print("DECODE FAILED for \(T.self): \(describe(error))")
            print("RAW: \(String(data: data.prefix(3000), encoding: .utf8) ?? "<not text>")")
            throw APIError(status: 0, message: describe(error))
        }
    }

    struct Empty: Decodable {}

    /// A decoding failure in words that name the field, so a mismatch is a one-line fix.
    private func describe(_ error: DecodingError) -> String {
        func path(_ context: DecodingError.Context) -> String {
            context.codingPath.map(\.stringValue).joined(separator: ".")
        }
        switch error {
        case .keyNotFound(let key, let c):
            return "missing field \"\(key.stringValue)\" at \(path(c))"
        case .typeMismatch(let type, let c):
            return "field \(path(c)) isn't a \(type)"
        case .valueNotFound(let type, let c):
            return "field \(path(c)) was null but we need a \(type)"
        case .dataCorrupted(let c):
            return "bad value at \(path(c)): \(c.debugDescription)"
        @unknown default:
            return error.localizedDescription
        }
    }

    // MARK: Calls the app makes

    func signInWithApple(identityToken: String) async throws -> String {
        struct Out: Decodable { let session: String }
        let r = try request("POST", "auth/apple/native", body: ["identity_token": identityToken])
        return try await send(r, as: Out.self).session
    }

    func me() async throws -> Me {
        try await send(try request("GET", "me"), as: Me.self)
    }

    func analysis(scope: String = "all") async throws -> Analysis {
        try await send(try request("GET", "analysis", query: ["scope": scope]), as: Analysis.self)
    }

    func researchRoutes() async throws -> [ResearchRoute] {
        try await send(try request("GET", "research/routes"), as: [ResearchRoute].self)
    }

    func researchData(routeID: Int) async throws -> ResearchData {
        try await send(try request("GET", "research/routes/\(routeID)/data"), as: ResearchData.self)
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
