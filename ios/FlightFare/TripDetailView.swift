import SwiftUI
import Charts

struct TripDetailView: View {
    @State var trip: Trip
    let store: TripStore

    @State private var editingTarget = false
    @State private var targetText = ""
    @State private var problem: String?

    var body: some View {
        List {
            Section {
                PriceHeadline(trip: trip)
                    .listRowInsets(EdgeInsets(top: 12, leading: 16, bottom: 4, trailing: 16))
            }

            if trip.history.count >= 2 {
                Section("Price history") {
                    PriceChart(history: trip.history, target: trip.alertBelow)
                        .frame(height: 210)
                        .padding(.vertical, 8)
                }
            }

            Section("Trip") {
                Detail("Route", "\(trip.origin) → \(trip.destination)")
                Detail("Out", trip.travelDate.prettyDate)
                if let back = trip.returnDate { Detail("Back", back.prettyDate) }
                if let airline = trip.airline { Detail("Airline", airline) }
                if let latest = trip.latest {
                    if let stops = latest.stops {
                        Detail("Stops", stops == 0 ? "Nonstop" : "\(stops)")
                    }
                    if let flights = latest.flights, !flights.isEmpty { Detail("Flights", flights) }
                    if let out = latest.departTime, let back = latest.arriveTime {
                        Detail("Times", "\(out) → \(back)")
                    }
                }
            }

            Section {
                Button {
                    targetText = trip.alertBelow.map(String.init) ?? ""
                    editingTarget = true
                } label: {
                    HStack {
                        Text("Notify me below")
                        Spacer()
                        Text(trip.alertBelow.map { "$\($0)" } ?? "Off")
                            .foregroundStyle(.secondary)
                    }
                }
            } footer: {
                Text("We'll notify you the moment the price crosses below this, and once a day with everything that moved.")
            }

            if let problem {
                Section { Text(problem).font(.footnote).foregroundStyle(.red) }
            }
        }
        .navigationTitle(trip.name)
        .navigationBarTitleDisplayMode(.inline)
        .alert("Notify me below", isPresented: $editingTarget) {
            TextField("Price in dollars", text: $targetText).keyboardType(.numberPad)
            Button("Cancel", role: .cancel) {}
            Button("Save") { saveTarget() }
        } message: {
            Text("Leave it empty to turn alerts off for this trip.")
        }
    }

    private func saveTarget() {
        let below = Int(targetText.trimmingCharacters(in: .whitespaces))
        Task {
            do {
                let updated = try await API.shared.setAlert(tripID: trip.id, below: below)
                await MainActor.run {
                    trip = updated
                    store.replace(updated)
                    problem = nil
                }
            } catch {
                await MainActor.run { problem = error.localizedDescription }
            }
        }
    }
}

private struct Detail: View {
    let name: String, value: String
    init(_ name: String, _ value: String) { self.name = name; self.value = value }

    var body: some View {
        HStack {
            Text(name).foregroundStyle(.secondary)
            Spacer()
            Text(value).multilineTextAlignment(.trailing)
        }
        .font(.subheadline)
    }
}

private struct PriceHeadline: View {
    let trip: Trip

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let latest = trip.latest {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(latest.price, format: .currency(code: "USD").precision(.fractionLength(0)))
                        .font(.system(size: 40, weight: .semibold, design: .rounded).monospacedDigit())
                    ChangeLabel(change: trip.change)
                }
                Text(footnote(latest))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Text(trip.checkPending == true ? "Checking the price now…" : "No price recorded yet")
                    .font(.headline)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func footnote(_ latest: PricePoint) -> String {
        var line = "Checked \(latest.scrapedAt.formatted(.relative(presentation: .named)))"
        if let low = trip.lowest, low.price < latest.price {
            line += "  ·  lowest seen $\(Int(low.price))"
        }
        return line
    }
}

/// One series, so no legend — the section title names it. The newest point is labelled
/// directly; the rest are readable by dragging along the line.
private struct PriceChart: View {
    let history: [PricePoint]
    let target: Int?

    @State private var selected: PricePoint?

    var body: some View {
        Chart {
            ForEach(history) { point in
                LineMark(x: .value("Date", point.scrapedAt), y: .value("Price", point.price))
                    .interpolationMethod(.monotone)
                    .lineStyle(StrokeStyle(lineWidth: 2))
                    .foregroundStyle(.tint)
            }
            if let target {
                RuleMark(y: .value("Target", target))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 4]))
                    .foregroundStyle(.secondary.opacity(0.6))
                    .annotation(position: .top, alignment: .leading) {
                        Text("target $\(target)").font(.caption2).foregroundStyle(.secondary)
                    }
            }
            if let point = selected ?? history.last {
                PointMark(x: .value("Date", point.scrapedAt), y: .value("Price", point.price))
                    .symbolSize(90)
                    .foregroundStyle(.tint)
                    .annotation(position: .top, spacing: 6, overflowResolution: .init(x: .fit, y: .disabled)) {
                        VStack(spacing: 1) {
                            Text(point.price, format: .currency(code: "USD").precision(.fractionLength(0)))
                                .font(.caption.weight(.semibold).monospacedDigit())
                            Text(point.scrapedAt.formatted(.dateTime.day().month(.abbreviated)))
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.horizontal, 7).padding(.vertical, 4)
                        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 6))
                    }
            }
        }
        .chartYScale(domain: .automatic(includesZero: false))
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
        .chartXAxis {
            AxisMarks(preset: .aligned) { _ in
                AxisValueLabel(format: .dateTime.day().month(.abbreviated))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
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
                                guard let date: Date = proxy.value(atX: x) else { return }
                                selected = history.min {
                                    abs($0.scrapedAt.timeIntervalSince(date)) < abs($1.scrapedAt.timeIntervalSince(date))
                                }
                            }
                            .onEnded { _ in selected = nil }
                    )
            }
        }
    }
}
