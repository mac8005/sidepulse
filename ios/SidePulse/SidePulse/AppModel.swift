import Foundation
import UIKit

@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published var pushToken: String {
        didSet {
            UserDefaults.standard.set(pushToken, forKey: Defaults.pushToken)
            mirrorFocusStatusSettings()
        }
    }

    @Published var selectedFolderPath: String = "No USB folder selected"
    @Published var hasFolderAccess: Bool = false

    @Published var ledText: String {
        didSet { UserDefaults.standard.set(ledText, forKey: Defaults.ledText) }
    }

    @Published var serverBaseURL: String {
        didSet { UserDefaults.standard.set(serverBaseURL, forKey: Defaults.serverBaseURL) }
    }

    @Published var sharedSecret: String {
        didSet { UserDefaults.standard.set(sharedSecret, forKey: Defaults.sharedSecret) }
    }

    @Published var liveMonitorEnabled: Bool {
        didSet { UserDefaults.standard.set(liveMonitorEnabled, forKey: Defaults.liveMonitorEnabled) }
    }

    @Published var liveMonitorServerURL: String {
        didSet {
            UserDefaults.standard.set(liveMonitorServerURL, forKey: Defaults.liveMonitorServerURL)
            mirrorFocusStatusSettings()
        }
    }

    // SidePulse Dot behaviour.
    @Published var dotBrightness: Int {
        didSet {
            let value = DotBrightness.clamped(dotBrightness)
            if value != dotBrightness {
                dotBrightness = value
            }
            DotBrightness.configuredValue = value
        }
    }

    @Published var dotAppearance: DotAppearance {
        didSet {
            let value = dotAppearance.normalized
            if value != dotAppearance {
                dotAppearance = value
            }
            persistDotAppearance(value)
        }
    }

    @Published var showFinishedEnabled: Bool {
        didSet { UserDefaults.standard.set(showFinishedEnabled, forKey: Defaults.showFinishedEnabled) }
    }

    @Published var dndEnabled: Bool {
        didSet { UserDefaults.standard.set(dndEnabled, forKey: Defaults.dndEnabled) }
    }

    @Published var dndScheduleEnabled: Bool {
        didSet {
            UserDefaults.standard.set(dndScheduleEnabled, forKey: Defaults.dndScheduleEnabled)
            rearmDndSchedule()
        }
    }

    @Published var dndStartTime: String {
        didSet {
            UserDefaults.standard.set(dndStartTime, forKey: Defaults.dndStartTime)
            rearmDndSchedule()
        }
    }

    @Published var dndEndTime: String {
        didSet {
            UserDefaults.standard.set(dndEndTime, forKey: Defaults.dndEndTime)
            rearmDndSchedule()
        }
    }

    @Published var dndLastScheduleTransition: String {
        didSet { UserDefaults.standard.set(dndLastScheduleTransition, forKey: Defaults.dndLastScheduleTransition) }
    }

    /// Keep the Dot off while an iOS Focus (Sleep, Do Not Disturb, …) is on.
    @Published var focusDndEnabled: Bool {
        didSet {
            UserDefaults.standard.set(focusDndEnabled, forKey: Defaults.focusDndEnabled)
            mirrorFocusStatusSettings()
        }
    }

    @Published var lastMessage: String = "Ready"
    @Published var eventLog: [String] = []
    @Published var receivedPushes: [ReceivedPush] {
        didSet { persistReceivedPushes() }
    }

    private enum Defaults {
        static let pushToken = "pushToken"
        static let ledText = "ledText"
        static let serverBaseURL = "serverBaseURL"
        static let sharedSecret = "sharedSecret"
        static let receivedPushes = "receivedPushes"
        static let liveMonitorEnabled = "liveMonitorEnabled"
        static let liveMonitorServerURL = "liveMonitorServerURL"
        static let dotAnimation = "dotAnimation"
        static let dotWorkingColor = "dotWorkingColor"
        static let dotNeedsInputColor = "dotNeedsInputColor"
        static let dotFinishedColor = "dotFinishedColor"
        static let kittModeEnabled = "kittModeEnabled"
        static let showFinishedEnabled = "showFinishedEnabled"
        static let dndEnabled = "dndEnabled"
        static let dndScheduleEnabled = "dndScheduleEnabled"
        static let dndStartTime = "dndStartTime"
        static let dndEndTime = "dndEndTime"
        static let dndLastScheduleTransition = "dndLastScheduleTransition"
        static let focusDndEnabled = "focusDndEnabled"
    }

    private init() {
        let defaults = UserDefaults.standard
        self.pushToken = defaults.string(forKey: Defaults.pushToken) ?? ""
        self.ledText = defaults.string(forKey: Defaults.ledText) ?? """
        #404040 1.4s pulse
        off 400ms none
        repeat
        """
        self.serverBaseURL = defaults.string(forKey: Defaults.serverBaseURL) ?? "http://127.0.0.1:8787"
        self.sharedSecret = defaults.string(forKey: Defaults.sharedSecret) ?? ""
        self.liveMonitorEnabled = defaults.bool(forKey: Defaults.liveMonitorEnabled)
        self.liveMonitorServerURL = defaults.string(forKey: Defaults.liveMonitorServerURL)
            ?? "http://macmini8005:8787"
        self.dotBrightness = DotBrightness.configuredValue
        let savedAnimation = defaults.string(forKey: Defaults.dotAnimation)
            .flatMap(DotAnimation.init(rawValue:))
        let animation = savedAnimation
            ?? (defaults.bool(forKey: Defaults.kittModeEnabled) ? .kitt : .gentle)
        self.dotAppearance = DotAppearance(
            animation: animation,
            workingColor: defaults.string(forKey: Defaults.dotWorkingColor),
            needsInputColor: defaults.string(forKey: Defaults.dotNeedsInputColor),
            finishedColor: defaults.string(forKey: Defaults.dotFinishedColor)
        )
        self.showFinishedEnabled = defaults.bool(forKey: Defaults.showFinishedEnabled)
        self.dndEnabled = defaults.bool(forKey: Defaults.dndEnabled)
        self.dndScheduleEnabled = defaults.bool(forKey: Defaults.dndScheduleEnabled)
        self.dndStartTime = defaults.string(forKey: Defaults.dndStartTime) ?? DndSchedule.defaultStartTime
        self.dndEndTime = defaults.string(forKey: Defaults.dndEndTime) ?? DndSchedule.defaultEndTime
        self.dndLastScheduleTransition = defaults.string(forKey: Defaults.dndLastScheduleTransition) ?? ""
        self.focusDndEnabled = defaults.bool(forKey: Defaults.focusDndEnabled)
        self.receivedPushes = Self.loadReceivedPushes()
        self.eventLog = EventLog.entries()
        persistDotAppearance(dotAppearance)
        refreshFolderStatus()
        mirrorFocusStatusSettings()
    }

    func resetDotAppearance() {
        dotAppearance = .defaults
    }

    private func persistDotAppearance(_ appearance: DotAppearance) {
        let defaults = UserDefaults.standard
        defaults.set(appearance.animation.rawValue, forKey: Defaults.dotAnimation)
        defaults.set(appearance.workingColor, forKey: Defaults.dotWorkingColor)
        defaults.set(appearance.needsInputColor, forKey: Defaults.dotNeedsInputColor)
        defaults.set(appearance.finishedColor, forKey: Defaults.dotFinishedColor)
        // Keep the legacy setting current so an older build still selects the
        // closest available working animation after a downgrade.
        defaults.set(appearance.animation == .kitt, forKey: Defaults.kittModeEnabled)
    }

    /// Port of `apply_due_dnd_schedule`: once a schedule boundary has passed,
    /// flip DND to that boundary's state — exactly once, so a manual override
    /// afterwards sticks until the next boundary.
    @discardableResult
    func applyDueDndSchedule(now: Date = Date()) -> Bool {
        guard dndScheduleEnabled,
              let transition = DndSchedule.latestTransition(startTime: dndStartTime, endTime: dndEndTime, now: now),
              transition.key != dndLastScheduleTransition
        else { return false }
        dndLastScheduleTransition = transition.key
        if dndEnabled != transition.enabled {
            dndEnabled = transition.enabled
        }
        return true
    }

    /// Editing the schedule applies its current phase right away, like the
    /// Mac app's Save Schedule button.
    private func rearmDndSchedule() {
        dndLastScheduleTransition = ""
        applyDueDndSchedule()
    }

    var dndStatusText: String {
        let status = dndEnabled ? "DND is on. LEDs are off." : "DND is off."
        guard dndScheduleEnabled else { return status }
        return "\(status) Schedule: \(dndStartTime)–\(dndEndTime)."
    }

    func setPushToken(from deviceToken: Data) {
        pushToken = deviceToken.map { String(format: "%02x", $0) }.joined()
        EventLog.append("APNs token updated")
        lastMessage = "Push token updated"
        refreshEventLog()
    }

    func refreshFolderStatus() {
        hasFolderAccess = DriveWriter.shared.hasSavedFolder
        selectedFolderPath = DriveWriter.shared.savedFolderDisplayName
    }

    func recordWriteSuccess(_ message: String) {
        EventLog.append(message)
        lastMessage = message
        refreshFolderStatus()
        refreshEventLog()
    }

    func recordError(_ error: Error) {
        let message = error.localizedDescription
        EventLog.append("Error: \(message)")
        lastMessage = message
        refreshFolderStatus()
        refreshEventLog()
    }

    func recordReceivedPush(_ push: ReceivedPush) {
        var next = [push]
        next.append(contentsOf: receivedPushes)
        if next.count > 50 {
            next.removeLast(next.count - 50)
        }
        receivedPushes = next

        let status = push.writeStatus.displayName
        EventLog.append("\(push.source): \(push.title) (\(status))")
        lastMessage = "\(push.title) - \(status)"
        refreshFolderStatus()
        refreshEventLog()
    }

    func clearReceivedPushes() {
        receivedPushes = []
        lastMessage = "Cleared received pushes"
    }

    func refreshEventLog() {
        eventLog = EventLog.entries()
    }

    func clearEventLog() {
        EventLog.clear()
        refreshEventLog()
    }

    var preauthenticatedPostURL: String? {
        let trimmedBase = serverBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !trimmedBase.isEmpty, !pushToken.isEmpty else {
            return nil
        }

        var components = URLComponents(string: trimmedBase + "/v1/push")
        var items = [
            URLQueryItem(name: "device_token", value: pushToken)
        ]

        if !sharedSecret.isEmpty {
            items.append(URLQueryItem(name: "key", value: sharedSecret))
        }

        components?.queryItems = items
        return components?.url?.absoluteString
    }

    var pushEndpointURL: String? {
        let trimmedBase = serverBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !trimmedBase.isEmpty else {
            return nil
        }
        return trimmedBase + "/v1/push"
    }

    var curlExample: String? {
        guard let pushEndpointURL else {
            return nil
        }

        let tokenLine = pushToken.isEmpty ? "" : "\n  -d '{\"device_token\":\"\(pushToken)\",\"pattern\":\"green_pulse_2\"}'"
        let authHeader = sharedSecret.isEmpty ? "" : " \\\n  -H \"Authorization: Bearer \(sharedSecret)\""
        if tokenLine.isEmpty {
            return """
            curl -X POST \(pushEndpointURL)\(authHeader) \\
              -H "content-type: application/json" \\
              -d '{"pattern":"green_pulse_2"}'
            """
        }

        return """
        curl -X POST \(pushEndpointURL)\(authHeader) \\
          -H "content-type: application/json" \(tokenLine)
        """
    }

    var shortcutWriteURL: String? {
        guard !ledText.isEmpty else {
            return nil
        }

        var components = URLComponents()
        components.scheme = "sidepulse"
        components.host = "write"
        components.queryItems = [
            URLQueryItem(name: "text", value: ledText)
        ]
        return components.url?.absoluteString
    }

    func shortcutPatternURL(for pattern: LEDPattern) -> String? {
        var components = URLComponents()
        components.scheme = "sidepulse"
        components.host = "write"
        components.queryItems = [
            URLQueryItem(name: "pattern", value: pattern.name)
        ]
        return components.url?.absoluteString
    }

    private func persistReceivedPushes() {
        if let data = try? JSONEncoder().encode(receivedPushes) {
            UserDefaults.standard.set(data, forKey: Defaults.receivedPushes)
        }
    }

    private func mirrorFocusStatusSettings() {
        FocusStatusShared.store(
            serverURL: liveMonitorServerURL,
            pushToken: pushToken,
            isEnabled: focusDndEnabled
        )
    }

    private static func loadReceivedPushes() -> [ReceivedPush] {
        guard let data = UserDefaults.standard.data(forKey: Defaults.receivedPushes),
              let pushes = try? JSONDecoder().decode([ReceivedPush].self, from: data) else {
            return []
        }
        return Array(pushes.prefix(50))
    }
}

extension ReceivedPush.WriteStatus {
    var displayName: String {
        switch self {
        case .received:
            return "Received"
        case .wrote:
            return "Wrote LEDS.LED"
        case .noFolder:
            return "Folder needed"
        case .failed:
            return "Failed"
        case .unsupportedPattern:
            return "Unknown pattern"
        }
    }
}
