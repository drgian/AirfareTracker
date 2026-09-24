import SwiftUI
import Charts

@MainActor
final class ResearchStore: ObservableObject {
    @Published var routes: [ResearchRoute] = []
    @Published var problem: String?
    @Published var loading = false

    func load() async {
        loading = routes.isEmpty
        defer { loading = false }
        do {
            routes = try await API.shared.researchRoutes()
            problem = nil
        } catch let error as APIError where error.isUnauthorized {
            Session.shared.end()
        } catch {
            problem = error.localizedDescription
        }
    }
}

struct ResearchView: View {
    @StateObject private var store = ResearchStore()

    var body: some View {
        NavigationStack {
            Group {
                if store.loading {
                    ProgressView()
                } else if store.routes.isEmpty {
                    ContentUnavailableView {
                        Label(store.problem == nil ? "Nothing being studied" : "Couldn't load",
                              systemImage: store.problem == nil ? "chart.xyaxis.line" : "exclamationmark.triangle")
                    } description: {
                        Text(store.problem ?? "Set a route up at flightfare.io and we'll price the same trip every week, six months out.")
                    }
                } else {
                    List(store.routes) { route in
                        NavigationLink(value: route) { ResearchRow(route: route) }
                    }
                }
            }
            .navigationTitle("Research")
            .navigationDestination(for: ResearchRoute.self) { ResearchDetailView(route: $0) }
            .refreshable { await store.load() }
            .task { await store.load() }
        }
    }
}

private struct ResearchRow: View {
    let route: ResearchRoute

    var body: some View {
        HStack(spacing: 12) {
            AirlineLogo(code: route.airline)
            VStack(alignment: .leading, spacing: 3) {
                Text(route.label).font(.body.weight(.medium))
                Text("\(route.origin) → \(route.destination)  ·  \(route.summary)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                Text("\(route.priced)")
                    .font(.body.weight(.semibold).monospacedDigit())
                Text(route.waiting > 0 ? "\(route.waiting) queued" : "prices")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }
}

struct ResearchDetailView: View {
    let route: ResearchRoute

    @State private var points: [ResearchPoint] = []
    @State private var loading = true
    @State private var problem: String?

    /// Only the observations that actually returned a price. The rest are failed checks,
    /// which matter for diagnosing the scraper but would be holes in a curve.
    private var priced: [ResearchPoint] { points.filter { $0.price != nil } }
    private var failures: Int { points.count - priced.count }

    var body: some View {
        List {
            if loading {
                Section { HStack { Spacer(); ProgressView(); Spacer() } }
            } else if let problem {
                Section { Text(problem).font(.footnote).foregroundStyle(.red) }
            }

            if !priced.isEmpty {
                Section {
                    BookingCurve(points: priced)
                        .frame(height: 220)
                        .padding(.vertical, 8)
                } header: {
                    Text("Price by how far ahead you book")
                } footer: {
                    Text("Each dot is one check: what this trip cost that day, plotted against how many days before departure it was. Further right means booked earlier.")
                }

                Section("What it says so far") {
                    if let best = priced.min(by: { ($0.price ?? 0) < ($1.price ?? 0) }) {
                        StatRow("Cheapest seen", "$\(Int(best.price ?? 0))", detail: "\(best.daysOut) days ahead")
                    }
                    if let worst = priced.max(by: { ($0.price ?? 0) < ($1.price ?? 0) }) {
                        StatRow("Dearest seen", "$\(Int(worst.price ?? 0))", detail: "\(worst.daysOut) days ahead")
                    }
                    StatRow("Observations", "\(priced.count)",
                        detail: failures > 0 ? "\(failures) checks failed" : nil)
                    if let oil = priced.last?.oil {
                        StatRow("Oil (WTI)", "$\(String(format: "%.2f", oil))", detail: nil)
                    }
                }
            } else if !loading {
                Section {
                    ContentUnavailableView("No prices yet", systemImage: "hourglass",
                                           description: Text(route.waiting > 0
                                                             ? "\(route.waiting) checks are queued. Prices appear as they run."
                                                             : "Nothing has priced successfully yet."))
                }
            }

            Section("Route") {
                StatRow("Route", "\(route.origin) → \(route.destination)", detail: nil)
                StatRow("Airline", AirlineNames.full(route.airline), detail: nil)
                StatRow("Departs", "\(route.departsOn)s", detail: nil)
                StatRow("Trip length", "\(route.nights) nights", detail: nil)
                StatRow("Ladder", "\(route.weeks) weeks", detail: nil)
            }
        }
        .navigationTitle(route.label)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            do {
                points = try await API.shared.researchData(routeID: route.id).prices
            } catch {
                problem = error.localizedDescription
            }
            loading = false
        }
    }
}

private struct StatRow: View {
    let name: String, value: String, detail: String?
    init(_ name: String, _ value: String, detail: String?) {
        self.name = name; self.value = value; self.detail = detail
    }

    var body: some View {
        HStack {
            Text(name).foregroundStyle(.secondary)
            Spacer()
            VStack(alignment: .trailing, spacing: 1) {
                Text(value)
                if let detail { Text(detail).font(.caption2).foregroundStyle(.secondary) }
            }
        }
        .font(.subheadline)
    }
}

/// The question this whole feature exists to answer: does booking earlier cost less?
/// Days ahead on the x-axis, price on the y, one dot per check.
private struct BookingCurve: View {
    let points: [ResearchPoint]

    @State private var selected: ResearchPoint?

    private var cheapest: Double { points.compactMap(\.price).min() ?? 0 }

    var body: some View {
        Chart {
            ForEach(points) { point in
                if let price = point.price {
                    PointMark(x: .value("Days ahead", point.daysOut), y: .value("Price", price))
                        .symbolSize(60)
                        .foregroundStyle(price == cheapest ? Color.green : Color.accentColor)
                        .opacity(selected == nil || selected == point ? 1 : 0.35)
                }
            }
            if let selected, let price = selected.price {
                RuleMark(x: .value("Days ahead", selected.daysOut))
                    .lineStyle(StrokeStyle(lineWidth: 1))
                    .foregroundStyle(.secondary.opacity(0.4))
                    .annotation(position: .top, spacing: 4,
                                overflowResolution: .init(x: .fit, y: .disabled)) {
                        VStack(spacing: 1) {
                            Text("$\(Int(price))")
                                .font(.caption.weight(.semibold).monospacedDigit())
                            Text("\(selected.daysOut) days ahead")
                                .font(.caption2).foregroundStyle(.secondary)
                            Text(selected.departureDate.prettyDate)
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                        .padding(.horizontal, 7).padding(.vertical, 4)
                        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 6))
                    }
            }
        }
        .chartYScale(domain: .automatic(includesZero: false))
        .chartXAxis {
            AxisMarks { value in
                AxisGridLine().foregroundStyle(.secondary.opacity(0.15))
                AxisValueLabel {
                    if let days = value.as(Int.self) {
                        Text("\(days)d").font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks { value in
                AxisGridLine().foregroundStyle(.secondary.opacity(0.18))
                AxisValueLabel {
                    if let price = value.as(Double.self) {
                        Text("$\(Int(price))").font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .chartOverlay { proxy in
            GeometryReader { geo in
                Rectangle().fill(.clear).contentShape(Rectangle())
                    .gesture(
                        DragGesture(minimumDistance: 0)
                            .onChanged { drag in
                                guard let plot = proxy.plotFrame else { return }
                                let x = drag.location.x - geo[plot].origin.x
                                guard let days: Int = proxy.value(atX: x) else { return }
                                selected = points.min {
                                    abs($0.daysOut - days) < abs($1.daysOut - days)
                                }
                            }
                            .onEnded { _ in selected = nil }
                    )
            }
        }
    }
}
