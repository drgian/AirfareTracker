import SwiftUI

struct AddTripView: View {
    let store: TripStore

    @Environment(\.dismiss) private var dismiss
    @State private var draft = TripDraft()
    @State private var airlines: [Airline] = []
    @State private var alertText = ""
    @State private var problem: String?
    @State private var saving = false

    private var chosenAirline: Airline? { airlines.first { $0.code == draft.airline } }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    AirportField(title: "From", code: $draft.origin)
                    AirportField(title: "To", code: $draft.destination)
                }

                Section {
                    DatePicker("Out", selection: $draft.travelDate,
                               in: tomorrow..., displayedComponents: .date)
                    DatePicker("Back", selection: $draft.returnDate,
                               in: dayAfter(draft.travelDate)..., displayedComponents: .date)
                }

                Section {
                    Picker("Airline", selection: $draft.airline) {
                        ForEach(airlines) { Text($0.name).tag($0.code) }
                    }
                    if let cabins = chosenAirline?.cabins {
                        Picker("Cabin", selection: $draft.cabin) {
                            ForEach(cabins) { Text($0.label).tag($0.value) }
                        }
                    }
                } footer: {
                    Text("We check this airline's own website, so the price is the one you'd pay there.")
                }

                Section {
                    TextField("Name (optional)", text: $draft.label)
                        .textInputAutocapitalization(.words)
                    TextField("Notify me below (optional)", text: $alertText)
                        .keyboardType(.numberPad)
                    TextField("Flight numbers (optional)", text: $draft.flights)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                } footer: {
                    Text("Flight numbers like DL2547 / DL66 pin the trip to those exact flights. Leave it blank and we track the cheapest.")
                }

                if let message = problem ?? draft.problem {
                    Section {
                        Text(message)
                            .font(.footnote)
                            .foregroundStyle(problem == nil ? .secondary : Color.red)
                    }
                }
            }
            .navigationTitle("Add a trip")
            .navigationBarTitleDisplayMode(.inline)
            .disabled(saving)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .topBarTrailing) {
                    if saving {
                        ProgressView()
                    } else {
                        Button("Track") { save() }.disabled(draft.problem != nil)
                    }
                }
            }
            .task { await loadAirlines() }
            // Switching airline invalidates the cabin, since each sells its own
            .onChange(of: draft.airline) { _, code in
                if let a = airlines.first(where: { $0.code == code }) { draft.cabin = a.defaultCabin }
            }
            // Keep the return date after the departure date rather than rejecting it later
            .onChange(of: draft.travelDate) { _, out in
                if draft.returnDate <= out { draft.returnDate = dayAfter(out) }
            }
        }
    }

    private var tomorrow: Date { Calendar.current.date(byAdding: .day, value: 1, to: .now.startOfDay)! }
    private func dayAfter(_ date: Date) -> Date {
        Calendar.current.date(byAdding: .day, value: 1, to: date.startOfDay)!
    }

    private func loadAirlines() async {
        guard airlines.isEmpty else { return }
        do {
            let list = try await API.shared.airlines()
            await MainActor.run {
                airlines = list
                if let first = list.first(where: { $0.code == draft.airline }) ?? list.first {
                    draft.airline = first.code
                    draft.cabin = first.defaultCabin
                }
            }
        } catch {
            await MainActor.run { problem = error.localizedDescription }
        }
    }

    private func save() {
        problem = nil
        saving = true
        draft.alertBelow = Int(alertText.trimmingCharacters(in: .whitespaces))
        Task {
            do {
                _ = try await API.shared.addTrip(draft)
                await store.load()
                await MainActor.run { saving = false; dismiss() }
            } catch {
                await MainActor.run { problem = error.localizedDescription; saving = false }
            }
        }
    }
}
