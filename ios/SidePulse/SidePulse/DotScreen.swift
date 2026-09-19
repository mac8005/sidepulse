import SwiftUI

/// A small picture of what the Dot is doing right now: the two LEDs, in the
/// colours the app would write to them, breathing the way they would.
struct DotPreview: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var mirror = DotStatusMirror.shared
    @ObservedObject private var stream = DotStatusMirror.shared.stream
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var breathing = false

    var body: some View {
        HStack(spacing: 8) {
            ForEach(0..<2, id: \.self) { index in
                Circle()
                    .fill(color(index))
                    .frame(width: 14, height: 14)
                    .shadow(color: color(index).opacity(isOff ? 0 : 0.8), radius: breathing ? 7 : 2)
                    .opacity(isOff ? 0.25 : (breathing ? 1 : 0.7))
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(
            RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous)
                .fill(Color(.tertiarySystemFill))
        )
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.easeInOut(duration: 1.4).repeatForever(autoreverses: true)) {
                breathing = true
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Dot preview")
        .accessibilityValue(mirror.statusText)
    }

    private var isOff: Bool {
        model.dndEnabled || model.dotBrightness == 0
    }

    private var displayState: LedDisplayState {
        LedDisplayState.forMode(stream.snapshot?.aggregateMode ?? "idle_ready")
    }

    private func color(_ index: Int) -> Color {
        guard !isOff else { return Color(.systemGray3) }
        let appearance = model.dotAppearance
        switch displayState {
        case .ask: return Color(dotHex: appearance.needsInputColor)
        case .done: return Color(dotHex: appearance.finishedColor)
        case .working:
            return index == 0
                ? Color(dotHex: appearance.workingColor)
                : Color(dotHex: appearance.workingColor).opacity(0.55)
        case .idle: return Color(.systemGray3)
        }
    }
}

/// The row that stands for the Dot wherever the app lists it: preview, state
/// in words, and where it is writing.
struct DotStatusRow: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var mirror = DotStatusMirror.shared

    var body: some View {
        HStack(spacing: 12) {
            DotPreview(model: model)
            VStack(alignment: .leading, spacing: 2) {
                Text("SidePulse Dot")
                    .font(.body)
                Text(mirror.statusText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
        .contentShape(.rect)
        .accessibilityElement(children: .combine)
        .accessibilityHint("Opens the Dot settings")
    }
}

/// Everything about the light on the desk, in one place: what it is showing,
/// how it behaves, where it writes, and the patterns you can send by hand.
struct DotScreen: View {
    @ObservedObject var model: AppModel
    let showFolderPicker: () -> Void
    @ObservedObject private var mirror = DotStatusMirror.shared

    @Environment(\.horizontalSizeClass) private var horizontalSizeClass

    var body: some View {
        dot
            .navigationTitle("SidePulse Dot")
            .duoCompactTitle()
    }

    /// One form on a phone; where there are two panes the light itself and how
    /// it behaves stay together on one side, and the drive it writes to plus
    /// the patterns you can send by hand take the other.
    @ViewBuilder
    private var dot: some View {
        if horizontalSizeClass == .regular {
            DuoSplit {
                Form {
                    statusSection
                    behaviourSection
                }
            } secondary: {
                Form {
                    folderSection
                    patternsSection
                }
            }
        } else {
            Form {
                statusSection
                folderSection
                behaviourSection
                patternsSection
            }
        }
    }

    @ViewBuilder
    private var statusSection: some View {
        Section {
            HStack(spacing: 14) {
                DotPreview(model: model)
                VStack(alignment: .leading, spacing: 3) {
                    Text(mirror.statusText)
                        .font(.headline)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(model.hasFolderAccess ? "Writing LEDS.LED" : "No folder yet")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 4)
            .accessibilityElement(children: .combine)
        }
    }

    @ViewBuilder
    private var folderSection: some View {
        Section {
            LabeledContent("Folder") {
                Text(model.selectedFolderPath)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.head)
            }
            Button {
                showFolderPicker()
            } label: {
                Label(
                    model.hasFolderAccess ? "Change LED folder" : "Choose the Dot's folder",
                    systemImage: "folder.badge.plus"
                )
            }
        } footer: {
            Text("SidePulse writes LEDS.LED on the Dot's USB drive while the app is open, and from a silent push while it is not.")
        }
    }

    @ViewBuilder
    private var behaviourSection: some View {
        Section("Behaviour") {
            DotBehaviorControls(model: model)
        }
    }

    @ViewBuilder
    private var patternsSection: some View {
        Section {
            QuickPatternsGrid { pattern in
                Task { await write(pattern) }
            }
            .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
        } header: {
            Text("Send a pattern")
        } footer: {
            Text("Writes the pattern to the Dot straight away, without waiting for an agent.")
        }
    }

    private func write(_ pattern: LEDPattern) async {
        do {
            let targetURL = try await DriveWriter.shared.write(pattern.ledText)
            model.recordWriteSuccess("Wrote \(targetURL.lastPathComponent)")
        } catch {
            model.recordError(error)
        }
    }
}

/// The hand-send patterns. An even number of columns wherever a crease exists,
/// so no tile is ever cut in half by the fold.
struct QuickPatternsGrid: View {
    let writePattern: (LEDPattern) -> Void
    @State private var fold = DuoFold()
    @State private var availableWidth: CGFloat = 0

    private var columns: [GridItem] {
        let count = duoColumnCount(
            availableWidth: availableWidth,
            minimumItemWidth: 150,
            spacing: 10,
            fold: fold
        )
        return Array(repeating: GridItem(.flexible(), spacing: 10), count: count)
    }

    var body: some View {
        LazyVGrid(columns: columns, spacing: 10) {
            ForEach(LEDPatternCatalog.patterns) { pattern in
                Button {
                    writePattern(pattern)
                } label: {
                    HStack(spacing: 9) {
                        Circle()
                            .fill(Color(dotHex: pattern.tintHex))
                            .frame(width: 12, height: 12)
                        Text(pattern.displayName)
                            .font(.subheadline)
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                        Spacer(minLength: 0)
                    }
                    .frame(minHeight: 44)
                    .padding(.horizontal, 12)
                    .background(
                        RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous)
                            .fill(Color(.tertiarySystemFill))
                    )
                }
                .buttonStyle(.plain)
                .accessibilityLabel(pattern.displayName)
                .accessibilityHint("Writes this pattern to the Dot")
            }
        }
        .duoFold($fold)
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { availableWidth = $0 }
    }
}

extension Color {
    /// "#4DA3FF" as the Dot stores it.
    init(dotHex hex: String) {
        let cleaned = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var value: UInt64 = 0
        Scanner(string: cleaned).scanHexInt64(&value)
        guard cleaned.count == 6 else {
            self = .accentColor
            return
        }
        self.init(
            .sRGB,
            red: Double((value >> 16) & 0xff) / 255,
            green: Double((value >> 8) & 0xff) / 255,
            blue: Double(value & 0xff) / 255,
            opacity: 1
        )
    }

    func dotHex(fallback: String) -> String {
        let color = UIColor(self)
        var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
        guard color.getRed(&red, green: &green, blue: &blue, alpha: &alpha) else { return fallback }
        let components = [red, green, blue].map { min(255, max(0, Int(($0 * 255).rounded()))) }
        return String(format: "#%02X%02X%02X", components[0], components[1], components[2])
    }
}
