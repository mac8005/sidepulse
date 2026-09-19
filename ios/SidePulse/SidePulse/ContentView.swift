import SwiftUI
import UIKit
import UserNotifications

/// Where the app starts and the only navigation it has: the board, with the
/// Dot and Settings one push away. There is no landing screen in front of the
/// thing people opened the app to see.
@MainActor
struct ContentView: View {
    @StateObject private var model: AppModel
    @State private var isShowingFolderPicker = false
    @State private var activeSheet: ActiveSheet?
    @State private var path: [Route] = []

    init() {
        _model = StateObject(wrappedValue: AppModel.shared)
        applyDemoScreen()
    }

    init(model: AppModel) {
        _model = StateObject(wrappedValue: model)
        applyDemoScreen()
    }

    private mutating func applyDemoScreen() {
#if DEBUG && SIDEPULSE_MAIN_APP
        switch DemoData.screen {
        case "dot": _path = State(initialValue: [.dot])
        case "settings": _path = State(initialValue: [.settings])
        case "token": _activeSheet = State(initialValue: .token)
        case "folder": _activeSheet = State(initialValue: .folderSetup)
        default: break
        }
#endif
    }

    var body: some View {
        NavigationStack(path: $path) {
            BoardScreen(model: model, path: $path)
                .navigationDestination(for: Route.self) { route in
                    switch route {
                    case .dot:
                        DotScreen(model: model, showFolderPicker: showFolderPicker)
                    case .settings:
                        SettingsView(
                            model: model,
                            requestPushToken: requestPushToken,
                            showFolderPicker: showFolderPicker,
                            showToken: { activeSheet = .token }
                        )
                    }
                }
        }
        .duoStripBehavior()
        .onOpenURL { url in
            if url.host == "agents" {
                path = []
            }
        }
        .sheet(item: $activeSheet) { sheet in
            switch sheet {
            case .token:
                TokenSheet(model: model, requestPushToken: requestPushToken)
            case .folderSetup:
                FolderSetupSheet { showFolderPicker() }
            }
        }
        .sheet(isPresented: $isShowingFolderPicker) {
            FolderPicker { url in
                isShowingFolderPicker = false
                do {
                    try DriveWriter.shared.saveFolder(url)
                    model.refreshFolderStatus()
                    model.lastMessage = "Selected \(url.lastPathComponent)"
                } catch {
                    model.recordError(error)
                }
            } onCancel: {
                isShowingFolderPicker = false
            }
        }
        .onAppear { model.refreshFolderStatus() }
    }

    private func requestPushToken() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { _, error in
            Task { @MainActor in
                if let error {
                    model.recordError(error)
                    return
                }
                UIApplication.shared.registerForRemoteNotifications()
                model.lastMessage = "Registering with APNs"
            }
        }
    }

    private func showFolderPicker() {
        activeSheet = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
            isShowingFolderPicker = true
        }
    }
}

enum Route: Hashable {
    case dot
    case settings
}

private enum ActiveSheet: Identifiable {
    case token
    case folderSetup

    var id: String {
        switch self {
        case .token: return "token"
        case .folderSetup: return "folderSetup"
        }
    }
}

// MARK: - Settings

private struct SettingsView: View {
    @ObservedObject var model: AppModel
    let requestPushToken: () -> Void
    let showFolderPicker: () -> Void
    let showToken: () -> Void
    @State private var diagnosticsExport: DiagnosticsExport?
    @State private var isShowingDiagnosticsError = false
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @Environment(\.verticalSizeClass) private var verticalSizeClass

    var body: some View {
        settings
            .navigationTitle("Settings")
            .duoCompactTitle()
            .alert("Couldn’t Prepare Log", isPresented: $isShowingDiagnosticsError) {
                Button("OK", role: .cancel) {}
            } message: {
                Text("Please try again. You can also select and copy individual log entries below.")
            }
    }

    /// One form on a phone; on a display wide enough for two panes the Mac
    /// half and the phone half sit side by side instead of one long scroll.
    @ViewBuilder
    private var settings: some View {
        if horizontalSizeClass == .regular {
            DuoSplit {
                Form { macSections }
            } secondary: {
                Form { phoneSections }
            }
        } else {
            Form {
                macSections
                phoneSections
            }
        }
    }

    @ViewBuilder
    private var macSections: some View {
        Section {
            TextField("http://your-mac.local:8787", text: $model.liveMonitorServerURL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
            Toggle("Live Activity for agents", isOn: $model.liveMonitorEnabled)
                .onChange(of: model.liveMonitorEnabled) { _, enabled in
                    if enabled { LiveMonitorManager.shared.start(model: model) }
                }
        } header: {
            Text("Monitored Mac")
        } footer: {
            Text("Run `sidepulse live-activity` on that Mac. It streams agent status to this phone and keeps the Lock Screen card current.")
        }

        Section("Notifications") {
            Button {
                requestPushToken()
            } label: {
                Label("Register for push", systemImage: "bell.badge")
            }
            LabeledContent("Push token") {
                Text(model.pushToken.isEmpty ? "None yet" : "Registered")
                    .foregroundStyle(.secondary)
            }
            Button {
                showToken()
            } label: {
                Label("Show token", systemImage: "key.horizontal")
            }
            .disabled(model.pushToken.isEmpty)
        }

        Section("SidePulse Dot") {
            LabeledContent("Folder") {
                Text(model.selectedFolderPath)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.head)
            }
            Button {
                showFolderPicker()
            } label: {
                Label("Choose folder", systemImage: "folder.badge.plus")
            }
        }
    }

    @ViewBuilder
    private var phoneSections: some View {
        Section {
            TextField("Proxy base URL", text: $model.serverBaseURL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
            SecureField("Shared secret", text: $model.sharedSecret)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            if let curlExample = model.curlExample {
                Button {
                    UIPasteboard.general.string = curlExample
                    model.lastMessage = "Copied curl example"
                } label: {
                    Label("Copy curl example", systemImage: "terminal")
                }
            }
        } header: {
            Text("Push proxy")
        } footer: {
            Text("Optional: lets anything that can send an HTTP request light the Dot.")
        }

        Section {
            TextEditor(text: $model.ledText)
                .font(.system(.body, design: .monospaced))
                .frame(minHeight: verticalSizeClass == .compact ? 80 : 120)
            Button {
                Task { await writeLocalTest() }
            } label: {
                Label("Write to the Dot", systemImage: "square.and.arrow.down")
            }
        } header: {
            Text("Raw LED program")
        }

        Section("Diagnostics") {
            Button {
                shareDiagnostics()
            } label: {
                Label("Share log", systemImage: "square.and.arrow.up")
            }
            .accessibilityHint("Shares a text file with recent events and current Dot settings")
            .popover(item: $diagnosticsExport) { export in
                DiagnosticsShareSheet(url: export.url)
            }
            Button {
                model.refreshEventLog()
            } label: {
                Label("Refresh log", systemImage: "arrow.clockwise")
            }
            Button(role: .destructive) {
                model.clearEventLog()
            } label: {
                Label("Clear log", systemImage: "trash")
            }
            if model.eventLog.isEmpty {
                Text("No events").foregroundStyle(.secondary)
            } else {
                ForEach(model.eventLog.reversed().prefix(40), id: \.self) { line in
                    Text(line)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                }
            }
        }
    }

    private func shareDiagnostics() {
        model.refreshEventLog()
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "unknown"
        let backgroundRefresh: String
        switch UIApplication.shared.backgroundRefreshStatus {
        case .available: backgroundRefresh = "available"
        case .denied: backgroundRefresh = "denied"
        case .restricted: backgroundRefresh = "restricted"
        @unknown default: backgroundRefresh = "unknown"
        }
        let details = [
            "App: \(version) (\(build))",
            "Device: \(UIDevice.current.model), iOS \(UIDevice.current.systemVersion)",
            "Time zone: \(TimeZone.current.identifier)",
            "Background App Refresh: \(backgroundRefresh)",
            "Low Power Mode: \(ProcessInfo.processInfo.isLowPowerModeEnabled)",
            "Dot folder saved: \(model.hasFolderAccess)",
            "Dot status: \(DotStatusMirror.shared.statusText)",
            "Brightness: \(model.dotBrightness)/\(DotBrightness.maximum)",
            "Animation: \(model.dotAppearance.animation.rawValue)",
            "Show finished: \(model.showFinishedEnabled)",
            "Completion notifications: \(model.dotCompletionAlertsEnabled)",
            "DND: \(model.dndEnabled)",
            "DND schedule: \(model.dndScheduleEnabled), \(model.dndStartTime)–\(model.dndEndTime)",
            "Off during Focus: \(model.focusDndEnabled)",
            "Live Activity enabled: \(model.liveMonitorEnabled)"
        ]
        do {
            let url = try EventLog.export(
                entries: model.eventLog,
                details: details,
                redacting: [model.pushToken, model.sharedSecret, model.selectedFolderPath]
            )
            diagnosticsExport = DiagnosticsExport(url: url)
        } catch {
            model.recordError(error)
            isShowingDiagnosticsError = true
        }
    }

    private func writeLocalTest() async {
        do {
            let targetURL = try await DriveWriter.shared.write(model.ledText)
            model.recordWriteSuccess("Wrote \(targetURL.lastPathComponent)")
        } catch {
            model.recordError(error)
        }
    }
}

private struct DiagnosticsExport: Identifiable {
    let url: URL
    var id: URL { url }
}

private struct DiagnosticsShareSheet: UIViewControllerRepresentable {
    let url: URL
    @Environment(\.dismiss) private var dismiss

    func makeUIViewController(context: Context) -> UIActivityViewController {
        let controller = UIActivityViewController(activityItems: [url], applicationActivities: nil)
        controller.completionWithItemsHandler = { _, _, _, _ in
            try? FileManager.default.removeItem(at: url)
            dismiss()
        }
        return controller
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}

// MARK: - Sheets

private struct TokenSheet: View {
    @ObservedObject var model: AppModel
    let requestPushToken: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Push token") {
                    if model.pushToken.isEmpty {
                        Text("No token yet").foregroundStyle(.secondary)
                    } else {
                        Text(model.pushToken)
                            .font(.system(.footnote, design: .monospaced))
                            .textSelection(.enabled)
                        Button {
                            UIPasteboard.general.string = model.pushToken
                            model.lastMessage = "Copied push token"
                        } label: {
                            Label("Copy token", systemImage: "doc.on.doc")
                        }
                    }
                    Button {
                        requestPushToken()
                    } label: {
                        Label("Request token", systemImage: "bell.badge")
                    }
                }
            }
            .navigationTitle("Push Token")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .duoHorizontalSheetBar()
        }
    }
}

private struct FolderSetupSheet: View {
    let openPicker: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                Label("The Dot in Files", systemImage: "externaldrive")
                    .font(.title3.weight(.semibold))
                VStack(alignment: .leading, spacing: 10) {
                    Text("1. Attach the SidePulse Dot to this iPhone.")
                    Text("2. Open Files and pick the drive's folder — the one that holds LEDS.LED.")
                    Text("3. SidePulse remembers it for pushes and Shortcuts.")
                }
                .font(.body)
                .foregroundStyle(.secondary)
                Button {
                    dismiss()
                    openPicker()
                } label: {
                    Label("Open Files", systemImage: "folder")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                Spacer()
            }
            .padding(20)
            .navigationTitle("Set Up Folder")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .duoHorizontalSheetBar()
        }
    }
}

// MARK: - Dot behaviour

/// The Dot's behaviour, shared by the Dot screen and the desk controls.
struct DotBehaviorControls: View {
    @ObservedObject var model: AppModel
    @State private var isAppearanceExpanded = false

    var body: some View {
        LabeledContent("Brightness", value: brightnessLabel)

        Slider(value: brightnessPercentage, in: 0...100, step: 1) {
            Text("SidePulse Dot brightness")
        } minimumValueLabel: {
            Image(systemName: "sun.min").accessibilityHidden(true)
        } maximumValueLabel: {
            Image(systemName: "sun.max").accessibilityHidden(true)
        }
        .accessibilityLabel("SidePulse Dot brightness")
        .accessibilityValue(brightnessLabel)

        DisclosureGroup("Appearance", isExpanded: $isAppearanceExpanded) {
            Picker("Animation", selection: animation) {
                ForEach(DotAnimation.allCases) { animation in
                    Text(animation.label).tag(animation)
                }
            }
            .pickerStyle(.menu)

            Text(model.dotAppearance.animation.detail)
                .font(.footnote)
                .foregroundStyle(.secondary)

            Picker("Colour palette", selection: palette) {
                ForEach(DotPalette.allCases) { palette in
                    Text(palette.label).tag(palette.rawValue)
                }
                if model.dotAppearance.palette == nil {
                    Text("Custom").tag("custom")
                }
            }
            .pickerStyle(.menu)

            ColorPicker("Working", selection: appearanceColor(\DotAppearance.workingColor, fallback: DotAppearance.defaultWorkingColor), supportsOpacity: false)
            ColorPicker("Needs input", selection: appearanceColor(\DotAppearance.needsInputColor, fallback: DotAppearance.defaultNeedsInputColor), supportsOpacity: false)
            ColorPicker("Finished", selection: appearanceColor(\DotAppearance.finishedColor, fallback: DotAppearance.defaultFinishedColor), supportsOpacity: false)

            Button("Reset appearance") { model.resetDotAppearance() }
        }

        Toggle("Keep finished sessions lit", isOn: $model.showFinishedEnabled)

        Toggle("Completion notifications", isOn: Binding(
            get: { model.dotCompletionAlertsEnabled },
            set: { model.setDotCompletionAlertsEnabled($0) }
        ))

        if let message = model.dotCompletionAlertsMessage {
            Text(message)
                .font(.footnote)
                .foregroundStyle(.orange)
        }

        Toggle("Do Not Disturb", isOn: $model.dndEnabled)
        Toggle("Off during iOS Focus", isOn: $model.focusDndEnabled)
        Toggle("Daily schedule", isOn: $model.dndScheduleEnabled)

        if model.dndScheduleEnabled {
            DndTimePicker("Start", time: $model.dndStartTime)
            DndTimePicker("End", time: $model.dndEndTime)
        }
    }

    private var brightnessPercentage: Binding<Double> {
        Binding {
            (Double(model.dotBrightness) / Double(DotBrightness.maximum) * 100).rounded()
        } set: { percentage in
            let percentage = min(100, max(0, percentage.rounded()))
            model.dotBrightness = DotBrightness.clamped(
                Int((percentage / 100 * Double(DotBrightness.maximum)).rounded())
            )
        }
    }

    private var animation: Binding<DotAnimation> {
        Binding {
            model.dotAppearance.animation
        } set: { animation in
            var appearance = model.dotAppearance
            appearance.animation = animation
            model.dotAppearance = appearance
        }
    }

    private var palette: Binding<String> {
        Binding {
            model.dotAppearance.palette?.rawValue ?? "custom"
        } set: { value in
            guard let palette = DotPalette(rawValue: value) else { return }
            var appearance = model.dotAppearance
            appearance.apply(palette)
            model.dotAppearance = appearance
        }
    }

    private func appearanceColor(
        _ keyPath: WritableKeyPath<DotAppearance, String>,
        fallback: String
    ) -> Binding<Color> {
        Binding {
            Color(dotHex: model.dotAppearance[keyPath: keyPath])
        } set: { color in
            var appearance = model.dotAppearance
            appearance[keyPath: keyPath] = color.dotHex(fallback: fallback)
            model.dotAppearance = appearance
        }
    }

    private var brightnessLabel: String {
        guard model.dotBrightness > 0 else { return "Off" }
        let percentage = Int(
            (Double(model.dotBrightness) / Double(DotBrightness.maximum) * 100).rounded()
        )
        return "\(percentage)%"
    }
}

/// Edits an "HH:MM" setting with the system time picker.
private struct DndTimePicker: View {
    let title: String
    @Binding var time: String

    init(_ title: String, time: Binding<String>) {
        self.title = title
        _time = time
    }

    var body: some View {
        DatePicker(title, selection: date, displayedComponents: .hourAndMinute)
    }

    private var date: Binding<Date> {
        Binding {
            let parsed = DndSchedule.parse(time) ?? (hour: 0, minute: 0)
            return Calendar.current.date(
                bySettingHour: parsed.hour, minute: parsed.minute, second: 0, of: Date()
            ) ?? Date()
        } set: { newValue in
            let components = Calendar.current.dateComponents([.hour, .minute], from: newValue)
            time = DndSchedule.format(hour: components.hour ?? 0, minute: components.minute ?? 0)
        }
    }
}
