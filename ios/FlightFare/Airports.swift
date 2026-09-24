import Foundation

struct Airport: Identifiable, Hashable {
    let code: String
    let name: String
    let city: String
    let country: String
    let size: Int
    /// Everything searchable, folded once at load time so typing stays cheap.
    let haystack: String

    var id: String { code }
    var label: String { "\(city) (\(code))" }
}

/// The same 4,000-odd airports the website uses, from the same file, ranked the same way —
/// so a search that works on the site works here.
@MainActor
final class Airports: ObservableObject {
    static let shared = Airports()

    @Published private(set) var all: [Airport] = []
    @Published private(set) var ready = false

    private let source = URL(string: "https://flightfare.io/airports.json")!
    private var cacheFile: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("airports.json")
    }

    /// Serve the cached copy straight away; refresh in the background if it's over a week old.
    /// The list barely changes, and 380KB isn't worth fetching on every launch.
    func load() async {
        if ready { return }
        let cached = try? Data(contentsOf: cacheFile)
        if let cached, let parsed = Self.parse(cached) {
            all = parsed
            ready = true
            if fresh { return }
        }
        guard let data = try? await URLSession.shared.data(from: source).0,
              let parsed = Self.parse(data) else { return }
        try? data.write(to: cacheFile)
        all = parsed
        ready = true
    }

    private var fresh: Bool {
        guard let modified = try? FileManager.default
            .attributesOfItem(atPath: cacheFile.path)[.modificationDate] as? Date else { return false }
        return Date().timeIntervalSince(modified) < 7 * 24 * 3600
    }

    private static func parse(_ data: Data) -> [Airport]? {
        // Rows are [code, name, city, country, size, extra] — a list, not an object, to keep the file small.
        guard let rows = try? JSONSerialization.jsonObject(with: data) as? [[Any]] else { return nil }
        return rows.compactMap { row in
            guard row.count >= 5,
                  let code = row[0] as? String, let name = row[1] as? String,
                  let city = row[2] as? String, let countryCode = row[3] as? String,
                  let size = row[4] as? Int else { return nil }
            let country = Locale.current.localizedString(forRegionCode: countryCode) ?? countryCode
            let extra = row.count > 5 ? (row[5] as? String ?? "") : ""
            return Airport(code: code, name: name, city: city, country: country, size: size,
                           haystack: fold("\(city) \(name) \(country) \(extra)"))
        }
    }

    /// Lowercase and strip accents, so "curacao" finds "Curaçao".
    static func fold(_ text: String) -> String {
        text.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: .current)
    }

    func search(_ query: String) -> [Airport] {
        let q = Self.fold(query.trimmingCharacters(in: .whitespaces))
        guard !q.isEmpty else { return [] }
        let up = q.uppercased()

        var scored: [(Int, Int, String, Airport)] = []
        for a in all {
            let score: Int?
            if a.code == up { score = 0 }
            else if q.count <= 3 && a.code.hasPrefix(up) { score = 1 }
            else if Self.fold(a.city).hasPrefix(q) { score = 2 }
            else if a.haystack.contains(" " + q) || a.haystack.hasPrefix(q) { score = 3 }
            else if q.count >= 3 && a.haystack.contains(q) { score = 4 }
            else { score = nil }
            if let score { scored.append((score, a.size, a.city, a)) }
        }
        scored.sort {
            if $0.0 != $1.0 { return $0.0 < $1.0 }
            if $0.1 != $1.1 { return $0.1 < $1.1 }
            return $0.2 < $1.2
        }
        return scored.prefix(8).map(\.3)
    }

    func named(_ code: String) -> Airport? { all.first { $0.code == code } }
}
