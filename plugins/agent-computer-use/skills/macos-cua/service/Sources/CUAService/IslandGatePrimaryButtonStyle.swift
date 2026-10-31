import SwiftUI

// AppKit's inactive-window tint must not hide the gate's primary action in release screenshots.
struct IslandGatePrimaryButtonStyle: ButtonStyle {
    var xBrand = false
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(xBrand ? Color.black : .white)
            .padding(.horizontal, 14)
            .frame(minHeight: 36)
            .frame(maxWidth: .infinity)
            .background(xBrand ? Color.white : Color.accentColor, in: RoundedRectangle(cornerRadius: 7))
            .contentShape(RoundedRectangle(cornerRadius: 7))
            .opacity(isEnabled ? (configuration.isPressed ? 0.85 : 1) : 0.35)
    }
}
