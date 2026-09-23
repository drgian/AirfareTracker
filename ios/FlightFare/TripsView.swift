import SwiftUI

@MainActor
final class TripStore: ObservableObject {
    @Published var trips: [Trip] = []
    @Published var problem: String?
    @Published var loading = false

    var active: [Trip] { trips.filter { !$0.archived } }
    var past: [Trip] { trips.filter(\.archived) }

    func load() async {
        loading = trips.isEmpty
        defer { loading = false }
        do {
            trips = try await API.shared.trips()
            problem = nil
        } catch let error as APIError where error.isUnauthorized {
            Session.shared.end()
        } catch {
            problem = error.localizedDescription
        }
    }

    func replace(_ trip: Trip) {
        if let i = trips.firstIndex(where: { $0.id == trip.id }) { trips[i] = trip }
    }
}

struct TripsView: View {
    @StateObject private var store = TripStore()
    @State private var showingSettings = false

    var body: some View {
        NavigationStack {
            Group {
                if store.loading {
                    ProgressView()
                } else if store.trips.isEmpty {
                    EmptyTrips(problem: store.problem)
                } else {
                    List {
                        if !store.active.isEmpty {
                            Section {
                                ForEach(store.active) { trip in
                                    NavigationLink(value: trip) { TripRow(trip: trip) }
                                }
                            }
                        }
                        if !store.past.isEmpty {
                            Section("Past") {
                                ForEach(store.past) { trip in
                                    NavigationLink(value: trip) { TripRow(trip: trip) }
                                }
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                }
            }
            .navigationTitle("Trips")
            .navigationDestination(for: Trip.self) { TripDetailView(trip: $0, store: store) }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showingSettings = true } label: {
                        Image(systemName: "person.crop.circle")
                    }
                }
            }
            .sheet(isPresented: $showingSettings) { SettingsView() }
            .refreshable { await store.load() }
            .task { await store.load() }
            .onReceive(NotificationCenter.default.publisher(for: .refreshTrips)) { _ in
                Task { await store.load() }
            }
        }
    }
}

private struct EmptyTrips: View {
    let problem: String?

    var body: some View {
        ContentUnavailableView {
            Label(problem == nil ? "No trips yet" : "Couldn't load your trips",
                  systemImage: problem == nil ? "airplane" : "exclamationmark.triangle")
        } description: {
            Text(problem ?? "Add a trip at flightfare.io and it'll show up here, with its price history.")
        }
    }
}

struct TripRow: View {
    let trip: Trip

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 3) {
                Text(trip.name).font(.body.weight(.medium))
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                if let latest = trip.latest {
                    Text(latest.price, format: .currency(code: "USD").precision(.fractionLength(0)))
                        .font(.body.weight(.semibold).monospacedDigit())
                    ChangeLabel(change: trip.change)
                } else {
                    Text(trip.checkPending == true ? "Checking…" : "No price yet")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 2)
    }

    private var subtitle: String {
        var bits = [trip.travelDate.prettyDate]
        if let back = trip.returnDate { bits.append(back.prettyDate) }
        var line = bits.joined(separator: " – ")
        if let airline = trip.airline { line = "\(airline)  \(line)" }
        if trip.isBelowTarget { line += "  ·  below your target" }
        return line
    }
}

struct ChangeLabel: View {
    let change: Double?

    var body: some View {
        if let change, change != 0 {
            let down = change < 0
            Label {
                Text(abs(change), format: .currency(code: "USD").precision(.fractionLength(0)))
            } icon: {
                Image(systemName: down ? "arrow.down" : "arrow.up")
            }
            .font(.caption.weight(.medium).monospacedDigit())
            .foregroundStyle(down ? Color.green : Color.red)
        } else {
            Text("no change").font(.caption).foregroundStyle(.secondary)
        }
    }
}

extension String {
    /// "2026-12-18" -> "Fri 18 Dec". Falls back to the raw text rather than showing nothing.
    var prettyDate: String {
        let iso = DateFormatter()
        iso.dateFormat = "yyyy-MM-dd"
        iso.timeZone = TimeZone(identifier: "UTC")
        guard let date = iso.date(from: self) else { return self }
        let out = DateFormatter()
        out.dateFormat = "EEE d MMM"
        out.timeZone = TimeZone(identifier: "UTC")
        return out.string(from: date)
    }
}
