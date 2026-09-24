import SwiftUI

/// A row that shows the chosen airport and opens a search sheet when tapped.
struct AirportField: View {
    let title: String
    @Binding var code: String

    @StateObject private var airports = Airports.shared
    @State private var picking = false

    var body: some View {
        Button { picking = true } label: {
            HStack {
                Text(title).foregroundStyle(.primary)
                Spacer()
                Text(chosen?.label ?? "Choose")
                    .foregroundStyle(code.isEmpty ? .secondary : .primary)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
        }
        .sheet(isPresented: $picking) {
            AirportSearch(title: title) { code = $0.code }
        }
        .task { await airports.load() }
    }

    private var chosen: Airport? { code.isEmpty ? nil : airports.named(code) }
}

struct AirportSearch: View {
    let title: String
    let onPick: (Airport) -> Void

    @StateObject private var airports = Airports.shared
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    var body: some View {
        NavigationStack {
            Group {
                if !airports.ready {
                    ProgressView("Loading airports…")
                } else if query.isEmpty {
                    ContentUnavailableView("Search airports", systemImage: "magnifyingglass",
                                           description: Text("By city, airport name or code — \u{201C}Grand Rapids\u{201D}, \u{201C}Cozumel\u{201D}, \u{201C}GRR\u{201D}."))
                } else {
                    let matches = airports.search(query)
                    if matches.isEmpty {
                        ContentUnavailableView.search(text: query)
                    } else {
                        List(matches) { airport in
                            Button {
                                onPick(airport)
                                dismiss()
                            } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    HStack(spacing: 8) {
                                        Text(airport.code)
                                            .font(.subheadline.weight(.semibold).monospaced())
                                            .foregroundStyle(.tint)
                                        Text(airport.city).font(.body)
                                    }
                                    Text("\(airport.name) · \(airport.country)")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            .buttonStyle(.plain)
                        }
                        .listStyle(.plain)
                    }
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always),
                        prompt: "City or airport code")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Cancel") { dismiss() } }
            }
            .task { await airports.load() }
        }
    }
}
