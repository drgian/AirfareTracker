import SwiftUI

/// An airline's own mark, on a light chip.
///
/// The chip isn't decoration: United's blue is #1414D2 and American's gradient runs to navy,
/// and the app is dark, so on the bare background those two would be all but invisible.
/// Giving every logo the same light ground also keeps four very different marks looking
/// like one set rather than four strays.
struct AirlineLogo: View {
    let code: String
    var size: CGFloat = 30

    private var asset: String? {
        let name = "logo-" + code.lowercased()
        return UIImage(named: name) == nil ? nil : name
    }

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: size * 0.22, style: .continuous)
                .fill(Color(white: 0.97))
            if let asset {
                Image(asset)
                    .resizable()
                    .scaledToFit()
                    .padding(size * 0.16)
            } else {
                // An airline we have no mark for still gets a consistent shape
                Text(code.uppercased())
                    .font(.system(size: size * 0.34, weight: .semibold, design: .rounded))
                    .foregroundStyle(.black.opacity(0.7))
                    .minimumScaleFactor(0.5)
                    .padding(2)
            }
        }
        .frame(width: size, height: size)
        .accessibilityLabel(Text(AirlineNames.full(code)))
    }
}

enum AirlineNames {
    private static let names = ["DL": "Delta", "AA": "American", "UA": "United", "WN": "Southwest"]

    static func full(_ code: String) -> String {
        names[code.uppercased()] ?? code.uppercased()
    }
}
