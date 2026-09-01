import SwiftUI
import UIKit
import UserNotifications

@MainActor
struct ContentView: View {
    @StateObject private var model: AppModel
    @State private var isShowingFolderPicker = false
    @State private var activeSheet: ActiveSheet?
    /// The app opens straight into Mac Agents; the home screen sits behind it.
    @State private var path: [Route] = [.agents]

    init() {
        _model = StateObject(wrappedValue: AppModel.shared)
    }

    init(model: AppModel) {
        _model = StateObject(wrappedValue: model)
    }

    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    MacAgentsPanel(model: model)

                    HeaderPanel(model: model) {
                        activeSheet = .token
                        requestPushToken()
                    }

                    SidePulseDotSetupPanel(model: model) {
                        activeSheet = .folderSetup
                    }

                    QuickPatternsPanel { pattern in
                        write(pattern)
                    }
                }
                .padding(16)
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("SidePulse")
            .navigationDestination(for: Route.self) { route in
                switch route {
                case .agents:
                    AgentsLiveView(model: model)
                }
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink {
                        SettingsView(
                            model: model,
                            requestPushToken: requestPushToken,
                            showFolderPicker: showFolderPicker
                        )
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("Settings")
                }
            }
        }
        .onOpenURL { url in
            if url.host == "agents" {
                path = [.agents]
            }
        }
        .sheet(item: $activeSheet) { sheet in
            switch sheet {
            case .token:
                TokenSheet(model: model, requestPushToken: requestPushToken)
            case .folderSetup:
                FolderSetupSheet {
                    showFolderPicker()
                }
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
        .onAppear {
            model.refreshFolderStatus()
        }
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

    private func write(_ pattern: LEDPattern) {
        let pushBase = ReceivedPush(
            source: "Quick Pattern",
            title: pattern.displayName,
            body: pattern.detail,
            patternName: pattern.name,
            ledText: pattern.ledText,
            payloadSummary: "{\"pattern\":\"\(pattern.name)\"}",
            writeStatus: .received
        )

        guard model.hasFolderAccess else {
            var push = pushBase
            push.writeStatus = .noFolder
            model.recordReceivedPush(push)
            activeSheet = .folderSetup
            return
        }

        do {
            let targetURL = try DriveWriter.shared.write(pattern.ledText)
            var push = pushBase
            push.body = "Wrote \(targetURL.lastPathComponent)"
            push.writeStatus = .wrote
            model.recordReceivedPush(push)
        } catch {
            var push = pushBase
            push.writeStatus = .failed
            push.errorMessage = error.localizedDescription
            model.recordReceivedPush(push)
        }
    }
}

private enum Route: Hashable {
    case agents
}

private enum ActiveSheet: Identifiable {
    case token
    case folderSetup

    var id: String {
        switch self {
        case .token:
            return "token"
        case .folderSetup:
            return "folderSetup"
        }
    }
}

private struct HeaderPanel: View {
    @ObservedObject var model: AppModel
    let getToken: () -> Void

    var body: some View {
        Panel {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Label("SidePulse", systemImage: "dot.radiowaves.left.and.right")
                            .font(.title2.weight(.semibold))
                        Text("Push inbox and SidePulse Dot writer")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }

                    Spacer()

                    StatusPill(isConnected: model.hasFolderAccess)
                }

                Button {
                    getToken()
                } label: {
                    Label(model.pushToken.isEmpty ? "Get Push Token" : "Show Push Token", systemImage: "key.horizontal")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
            }
        }
    }
}

private struct StatusPill: View {
    let isConnected: Bool

    var body: some View {
        Label(isConnected ? "Folder connected" : "No folder", systemImage: isConnected ? "checkmark.circle.fill" : "exclamationmark.circle")
            .font(.caption.weight(.semibold))
            .foregroundStyle(isConnected ? Color.green : Color.orange)
            .lineLimit(1)
    }
}

private struct SidePulseDotSetupPanel: View {
    @ObservedObject var model: AppModel
    let openSetup: () -> Void

    var body: some View {
        Panel {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "externaldrive")
                        .font(.title2)
                        .foregroundStyle(model.hasFolderAccess ? Color.green : Color.accentColor)
                        .frame(width: 34, height: 34)

                    VStack(alignment: .leading, spacing: 5) {
                        Text("SidePulse Dot Folder")
                            .font(.headline)
                        Text(model.selectedFolderPath)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }

                Button {
                    openSetup()
                } label: {
                    Label(model.hasFolderAccess ? "Change LED Folder" : "Set Up SidePulse Dot Folder", systemImage: "folder.badge.plus")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
            }
        }
    }
}

private struct QuickPatternsPanel: View {
    let writePattern: (LEDPattern) -> Void
    private let columns = [
        GridItem(.adaptive(minimum: 150), spacing: 10)
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Quick Patterns")
                .font(.headline)

            LazyVGrid(columns: columns, spacing: 10) {
                ForEach(LEDPatternCatalog.patterns) { pattern in
                    Button {
                        writePattern(pattern)
                    } label: {
                        PatternButtonLabel(pattern: pattern)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

private struct PatternButtonLabel: View {
    let pattern: LEDPattern

    var body: some View {
        Panel {
            HStack(spacing: 10) {
                Circle()
                    .fill(Color(hex: pattern.tintHex))
                    .frame(width: 12, height: 12)

                VStack(alignment: .leading, spacing: 3) {
                    Text(pattern.displayName)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                    Text(pattern.name)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
        }
    }
}

private struct TokenSheet: View {
    @ObservedObject var model: AppModel
    let requestPushToken: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Push Token") {
                    if model.pushToken.isEmpty {
                        Text("No token yet")
                            .foregroundStyle(.secondary)
                    } else {
                        Text(model.pushToken)
                            .font(.system(.footnote, design: .monospaced))
                            .textSelection(.enabled)

                        Button {
                            UIPasteboard.general.string = model.pushToken
                            model.lastMessage = "Copied push token"
                        } label: {
                            Label("Copy Token", systemImage: "doc.on.doc")
                        }
                    }

                    Button {
                        requestPushToken()
                    } label: {
                        Label("Request Token", systemImage: "bell.badge")
                    }
                }
            }
            .navigationTitle("Push Token")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") {
                        dismiss()
                    }
                }
            }
        }
    }
}

private struct FolderSetupSheet: View {
    let openPicker: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 18) {
                Label("SidePulse Dot Device in Files", systemImage: "externaldrive")
                    .font(.title3.weight(.semibold))

                VStack(alignment: .leading, spacing: 10) {
                    Text("1. Attach the SidePulse Dot USB drive to this iPhone or iPad.")
                    Text("2. Open Files and select the SidePulse Dot USB drive folder containing LEDS.LED.")
                    Text("3. SidePulse will remember that folder for later pushes and Shortcuts.")
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
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") {
                        dismiss()
                    }
                }
            }
        }
    }
}

private struct SettingsView: View {
    @ObservedObject var model: AppModel
    let requestPushToken: () -> Void
    let showFolderPicker: () -> Void

    var body: some View {
        Form {
            Section("Push Token") {
                Button {
                    requestPushToken()
                } label: {
                    Label("Get Push Token", systemImage: "key.horizontal")
                }

                if model.pushToken.isEmpty {
                    Text("No token yet")
                        .foregroundStyle(.secondary)
                } else {
                    Text(model.pushToken)
                        .font(.system(.footnote, design: .monospaced))
                        .textSelection(.enabled)

                    Button {
                        UIPasteboard.general.string = model.pushToken
                        model.lastMessage = "Copied push token"
                    } label: {
                        Label("Copy Token", systemImage: "doc.on.doc")
                    }
                }
            }

            Section("SidePulse Dot") {
                LabeledContent("Folder", value: model.selectedFolderPath)

                Button {
                    showFolderPicker()
                } label: {
                    Label("Set Up LED Folder", systemImage: "folder.badge.plus")
                }

                DotBehaviorControls(model: model)

                Toggle("Keep mirroring in background", isOn: $model.dotBackgroundEnabled)

                if model.dotBackgroundEnabled {
                    Text("Keeps SidePulse running after you switch away by playing silent audio, so the Dot follows the agents like the Live Activity. Uses more battery; the Dot pauses during calls and while another app plays audio.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }

            Section {
                Toggle("Use a daily schedule", isOn: $model.dndScheduleEnabled)

                if model.dndScheduleEnabled {
                    DndTimePicker("Start", time: $model.dndStartTime)
                    DndTimePicker("End", time: $model.dndEndTime)
                }
            } header: {
                Text("DND Schedule")
            } footer: {
                Text("The schedule switches DND on/off; you can override it at any time.")
            }

            Section("Live Monitor (Mac Agents)") {
                LiveMonitorSection(model: model)
            }

            Section("Advanced Server") {
                TextField("Proxy base URL", text: $model.serverBaseURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)

                SecureField("Shared secret", text: $model.sharedSecret)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()

                if let endpoint = model.pushEndpointURL {
                    Text(endpoint)
                        .font(.system(.footnote, design: .monospaced))
                        .textSelection(.enabled)
                }

                if let curlExample = model.curlExample {
                    Text(curlExample)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)

                    Button {
                        UIPasteboard.general.string = curlExample
                        model.lastMessage = "Copied curl example"
                    } label: {
                        Label("Copy Curl", systemImage: "terminal")
                    }
                }
            }

            Section("Raw LED Editor") {
                TextEditor(text: $model.ledText)
                    .font(.system(.body, design: .monospaced))
                    .frame(minHeight: 140)

                Button {
                    writeLocalTest()
                } label: {
                    Label("Write to USB", systemImage: "square.and.arrow.down")
                }

                if let shortcutURL = model.shortcutWriteURL {
                    Button {
                        UIPasteboard.general.string = shortcutURL
                        model.lastMessage = "Copied Shortcut URL"
                    } label: {
                        Label("Copy Shortcut URL", systemImage: "link.badge.plus")
                    }
                }
            }

            Section("Diagnostics") {
                Button {
                    model.refreshEventLog()
                } label: {
                    Label("Refresh Log", systemImage: "arrow.clockwise")
                }

                Button(role: .destructive) {
                    model.clearEventLog()
                } label: {
                    Label("Clear Log", systemImage: "trash")
                }

                if model.eventLog.isEmpty {
                    Text("No events")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(model.eventLog.reversed(), id: \.self) { line in
                        Text(line)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                    }
                }
            }

            Section("Inbox") {
                Button(role: .destructive) {
                    model.clearReceivedPushes()
                } label: {
                    Label("Clear Received Pushes", systemImage: "tray.and.arrow.down")
                }
            }
        }
        .navigationTitle("Settings")
    }

    private func writeLocalTest() {
        do {
            let targetURL = try DriveWriter.shared.write(model.ledText)
            model.recordWriteSuccess("Wrote \(targetURL.lastPathComponent)")
        } catch {
            model.recordError(error)
        }
    }
}

private struct Panel<Content: View>: View {
    @ViewBuilder let content: () -> Content

    var body: some View {
        content()
            .padding(14)
            .background(Color(.secondarySystemGroupedBackground))
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private extension Color {
    init(hex: String) {
        let cleaned = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var value: UInt64 = 0
        Scanner(string: cleaned).scanHexInt64(&value)

        let red: UInt64
        let green: UInt64
        let blue: UInt64

        switch cleaned.count {
        case 6:
            red = (value >> 16) & 0xff
            green = (value >> 8) & 0xff
            blue = value & 0xff
        default:
            red = 0x3b
            green = 0x82
            blue = 0xf6
        }

        self.init(
            .sRGB,
            red: Double(red) / 255,
            green: Double(green) / 255,
            blue: Double(blue) / 255,
            opacity: 1
        )
    }
}

private struct LiveMonitorSection: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var monitor = LiveMonitorManager.shared

    var body: some View {
        TextField("Monitor server URL", text: $model.liveMonitorServerURL)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .keyboardType(.URL)

        Toggle("Live Activity for Mac agents", isOn: $model.liveMonitorEnabled)
            .onChange(of: model.liveMonitorEnabled) { enabled in
                if enabled {
                    LiveMonitorManager.shared.start(model: model)
                }
            }
            .disabled(!monitor.isSupported)

        if !monitor.isSupported {
            Text("Requires iOS 17.2 or later.")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }

        Text(monitor.statusMessage)
            .font(.footnote)
            .foregroundStyle(.secondary)

        Text("Runs `sidepulse live-activity` on the Mac. The Mac starts a Live Activity on this phone whenever agents become active and streams their status to the Lock Screen and Dynamic Island.")
            .font(.caption)
            .foregroundStyle(.tertiary)
    }
}

/// KITT and DND toggles — the Mac app's menu-level Dot controls — plus the
/// iOS-only Focus option. Shared by the Mac Agents screen and Settings.
struct DotBehaviorControls: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var mirror = DotStatusMirror.shared

    var body: some View {
        Toggle("KITT scanner while working", isOn: $model.kittModeEnabled)

        Toggle("DND On", isOn: $model.dndEnabled)

        Toggle("Off during iOS Focus", isOn: $model.focusDndEnabled)

        if model.focusDndEnabled {
            Text("Counts every Focus that shares its status with SidePulse (iOS Settings › Focus › Sleep / Do Not Disturb › Focus Status).")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }

        Text(mirror.statusText)
            .font(.footnote)
            .foregroundStyle(.secondary)

        if model.dndScheduleEnabled {
            Text("Schedule: \(model.dndStartTime)–\(model.dndEndTime)")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
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

private struct MacAgentsPanel: View {
    @ObservedObject var model: AppModel

    var body: some View {
        NavigationLink(value: Route.agents) {
            HStack {
                Image(systemName: "desktopcomputer")
                    .font(.title3)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Mac Agents")
                        .font(.headline)
                    Text("Live view of agents on \(hostLabel)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            .padding(14)
            .background(Color(.secondarySystemGroupedBackground))
            .clipShape(RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
    }

    private var hostLabel: String {
        URL(string: model.liveMonitorServerURL)?.host ?? "the Mac"
    }
}
