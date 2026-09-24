import SwiftUI
import Charts

@MainActor
final class ResearchStore: ObservableObject {
    @Published var routes: [ResearchRoute] = []
    @Published var analysis: Analysis?
    @Published var problem: String?
    @Published var loading = false
    @Published var scope = "all"

    func load() async {
        loading = routes.isEmpty && analysis == nil
        defer { loading = false }
        // Kept separate on purpose: when the two were assigned together, an analysis
        // failure also blanked the list of routes that had loaded perfectly well.
        async let fetchedRoutes = API.shared.researchRoutes(scope: scope)
        async let fetchedAnalysis = API.shared.analysis(scope: scope)

        var trouble: String?
        do { routes = try await fetchedRoutes }
        catch let e as APIError where e.isUnauthorized { Session.shared.end(); return }
        catch { trouble = error.localizedDescription }

        do { analysis = try await fetchedAnalysis }
        catch let e as APIError where e.isUnauthorized { Session.shared.end(); return }
        catch { trouble = trouble ?? error.localizedDescription }

        problem = trouble
    }
}

struct ResearchView: View {
    @StateObject private var store = ResearchStore()
    @EnvironmentObject private var session: Session

    var body: some View {
        NavigationStack {
            Group {
                if store.loading {
                    ProgressView()
                } else {
                    List {
                        if let problem = store.problem {
                            Section { Text(problem).font(.footnote).foregroundStyle(.red) }
                        }
                        if let a = store.analysis, a.usable > 0 {
                            analysisSections(a)
                        }
                        routesSection
                    }
                }
            }
            .navigationTitle("Research")
            .toolbar {
                if session.role == "admin" {
                    ToolbarItem(placement: .topBarTrailing) {
                        Picker("Scope", selection: $store.scope) {
                            Text("Everyone").tag("all")
                            Text("Mine").tag("mine")
                        }
                        .pickerStyle(.segmented)
                    }
                }
            }
            .navigationDestination(for: ResearchRoute.self) { ResearchDetailView(route: $0) }
            .refreshable { await store.load() }
            .task { await store.load() }
            .onChange(of: store.scope) { _, _ in Task { await store.load() } }
        }
    }

    @ViewBuilder
    private func analysisSections(_ a: Analysis) -> some View {
        Section {
            StatRow("Prices recorded", "\(a.checks)",
                    detail: a.since.map { "since \($0.prettyDate)" })
            StatRow("Trips compared", "\(a.routes)", detail: nil)
        } header: {
            Text("What we have collected")
        } footer: {
            Text("Every bar below compares a price with that same trip's own average, so a cheap short hop and an expensive long haul can sit in the same chart.")
        }

        if a.byDaysAhead.contains(where: { $0.relative != nil }) {
            Section {
                RelativeBars(slices: a.byDaysAhead)
                    .frame(height: CGFloat(a.byDaysAhead.count) * 30 + 30)
                    .padding(.vertical, 6)
            } header: {
                Text("How far ahead you book")
            } footer: {
                Text("Green is cheaper than that trip's average, red is dearer. Research ladders count here too - each weekly rung is its own trip, priced over and over as it approaches.")
            }
        }

        if a.byWeekday.contains(where: { $0.relative != nil }) {
            Section {
                RelativeBars(slices: a.byWeekday)
                    .frame(height: CGFloat(a.byWeekday.count) * 30 + 30)
                    .padding(.vertical, 6)
            } header: {
                Text("Day of the week")
            } footer: {
                Text("Whether the day you look changes the price. Bars from fewer than ten checks are faded - there isn't enough there to trust yet.")
            }
        }

        Section {
            let c = a.changes
            StatRow("Checks compared", "\(c.pairs)",
                    detail: c.pairs > 0 ? "\(Int(c.steadyShare * 100))% unchanged" : nil)
            StatRow("Moved up", "\(c.ups)", detail: nil)
            StatRow("Moved down", "\(c.downs)", detail: nil)
            if let move = c.averageMove, let pct = c.averageMovePercent {
                StatRow("Typical move", "$\(Int(move))", detail: "\(String(format: "%.1f", pct * 100))%")
            }
            if let drop = c.biggestDrop, drop < 0 {
                StatRow("Biggest fall", "$\(Int(abs(drop)))", detail: nil)
            }
            if let rise = c.biggestRise, rise > 0 {
                StatRow("Biggest jump", "$\(Int(rise))", detail: nil)
            }
        } header: {
            Text("How often prices actually move")
        }
    }

    @ViewBuilder
    private var routesSection: some View {
        Section {
            if store.routes.isEmpty {
                Text("Set a route up at flightfare.io and we'll price the same trip every week, six months out.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(store.routes) { route in
                    NavigationLink(value: route) { ResearchRow(route: route) }
                }
            }
        } header: {
            Text("Routes being studied")
        }
    }
}

/// Above or below that trip's own average, so zero is the meaningful middle: one hue each
/// side and a neutral line between. Cheaper is green because cheaper is the good direction,
/// which is the same way the trip list reads a price fall.
private struct RelativeBars: View {
    let slices: [AnalysisSlice]

    var body: some View {
        Chart(slices) { slice in
            if let rel = slice.relative {
                BarMark(
                    xStart: .value("From", 0),
                    xEnd: .value("Difference", rel),
                    y: .value("Group", slice.label)
                )
                .foregroundStyle(rel <= 0 ? Color.green : Color.red)
                .opacity(slice.thin ? 0.35 : 0.9)
                .cornerRadius(3)
                .annotation(position: rel <= 0 ? .trailing : .leading, spacing: 5) {
                    Text(rel.asSignedPercent)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
        }
        .chartXAxis {
            AxisMarks { value in
                AxisGridLine().foregroundStyle(.secondary.opacity(0.15))
                AxisValueLabel {
                    if let d = value.as(Double.self) {
                        Text(d.asSignedPercent).font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks { value in
                AxisValueLabel {
                    if let name = value.as(String.self) {
                        Text(name).font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

extension Double {
    /// 0.032 -> "+3.2%". Anything that rounds to nothing is written as a flat "0.0%",
    /// because "-0.0%" reads like a real fall.
    var asSignedPercent: String {
        let pct = (self * 100 * 10).rounded() / 10
        return pct == 0 ? "0.0%" : String(format: "%+.1f%%", pct)
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
