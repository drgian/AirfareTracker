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

    func archive(_ trip: Trip) async {
        do { replace(try await API.shared.archive(tripID: trip.id)) }
        catch { problem = error.localizedDescription }
    }

    func restore(_ trip: Trip) async {
        do { replace(try await API.shared.restore(tripID: trip.id)) }
        catch { problem = error.localizedDescription }
    }

    func delete(_ trip: Trip) async {
        // Drop it from the list first; the API call is the slow part and it rarely fails.
        let previous = trips
        trips.removeAll { $0.id == trip.id }
        do { try await API.shared.deleteTrip(id: trip.id) }
        catch {
            trips = previous
            problem = error.localizedDescription
        }
    }
}

struct TripsView: View {
    @StateObject private var store = TripStore()
    @State private var showingSettings = false
    @State private var addingTrip = false
    @State private var deleting: Trip?

    var body: some View {
        NavigationStack {
            Group {
                if store.loading {
                    ProgressView()
                } else if store.trips.isEmpty {
                    EmptyTrips(problem: store.problem) { addingTrip = true }
                } else {
                    List {
                        if !store.active.isEmpty {
                            Section {
                                ForEach(store.active) { trip in
                                    NavigationLink(value: trip) { TripRow(trip: trip) }
                                        .swipeActions(edge: .trailing) {
                                            Button("Delete", role: .destructive) { deleting = trip }
                                            Button("Stop") { Task { await store.archive(trip) } }
                                                .tint(.orange)
                                        }
                                }
                            }
                        }
                        if !store.past.isEmpty {
                            Section("Past") {
                                ForEach(store.past) { trip in
                                    NavigationLink(value: trip) { TripRow(trip: trip) }
                                        .swipeActions(edge: .trailing) {
                                            Button("Delete", role: .destructive) { deleting = trip }
                                        }
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
                ToolbarItem(placement: .topBarLeading) {
                    Button { showingSettings = true } label: {
                        Image(systemName: "person.crop.circle")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { addingTrip = true } label: { Image(systemName: "plus") }
                }
            }
            .sheet(isPresented: $showingSettings) { SettingsView() }
            .sheet(isPresented: $addingTrip) { AddTripView(store: store) }
            .confirmationDialog("Delete this trip?", isPresented: .init(
                get: { deleting != nil }, set: { if !$0 { deleting = nil } }
            ), titleVisibility: .visible) {
                Button("Delete", role: .destructive) {
                    if let trip = deleting { Task { await store.delete(trip) } }
                    deleting = nil
                }
                Button("Cancel", role: .cancel) { deleting = nil }
            } message: {
                Text("Its price history goes too, and that can't be undone.")
            }
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
    let onAdd: () -> Void

    var body: some View {
        ContentUnavailableView {
            Label(problem == nil ? "No trips yet" : "Couldn't load your trips",
                  systemImage: problem == nil ? "airplane" : "exclamationmark.triangle")
        } description: {
            Text(problem ?? "Track a trip and we'll check its price every day, straight from the airline.")
        } actions: {
            if problem == nil { Button("Add a trip") { onAdd() } }
        }
    }
}

struct TripRow: View {
    let trip: Trip

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            AirlineLogo(code: trip.airline ?? "DL")
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
    /// "2026-12-18" -> "Fri 18 Dec". Also takes a full timestamp, since some fields carry
    /// one. Falls back to the raw text rather than showing nothing.
    var prettyDate: String {
        let iso = DateFormatter()
        iso.dateFormat = "yyyy-MM-dd"
        iso.timeZone = TimeZone(identifier: "UTC")
        let dayOnly = String(prefix(10))
        guard let date = iso.date(from: dayOnly) else { return self }
        let out = DateFormatter()
        out.dateFormat = "EEE d MMM"
        out.timeZone = TimeZone(identifier: "UTC")
        return out.string(from: date)
    }
}
