import Combine
import Foundation
import Intents
@preconcurrency import UserNotifications

/// LED display states, shared with the Mac status bar app (`led_status.py`).
enum LedDisplayState: Equatable {
    case idle
    case working
    case done
    case ask

    static func forMode(_ mode: String) -> LedDisplayState {
        switch mode {
        case "waiting_for_input", "blocked_error":
            return .ask
        case "working", "tool_running", "long_task_progress":
            return .working
        case "completed":
            return .done
        default:
            return .idle
        }
    }

    var label: String {
        switch self {
        case .idle: return "Idle"
        case .working: return "Working"
        case .done: return "Done"
        case .ask: return "Needs input"
        }
    }
}

enum DotPushApplyResult: Equatable {
    case written
    case alreadyCurrent
    case noFolder
    case failed

    var acknowledgementStatus: String {
        switch self {
        case .written: return "written"
        case .alreadyCurrent: return "alreadyCurrent"
        case .noFolder: return "noFolder"
        case .failed: return "failed"
        }
    }
}

/// The phone's current ability to accept Dot state changes. The retry lease
/// lets the daemon avoid spending silent pushes while the output is known to
/// be unavailable.
struct DotAvailability: Equatable {
    let available: Bool
    let reason: String?
    let retryAfterSeconds: Int?
    private let leaseIdentity: String

    static let ready = DotAvailability(
        available: true,
        reason: nil,
        retryAfterSeconds: nil,
        leaseIdentity: "available"
    )

    static func unavailable(
        reason: String,
        retryAfterSeconds: Int,
        leaseIdentity: String? = nil
    ) -> DotAvailability {
        DotAvailability(
            available: false,
            reason: reason,
            retryAfterSeconds: retryAfterSeconds,
            leaseIdentity: leaseIdentity ?? reason
        )
    }

    static func == (lhs: DotAvailability, rhs: DotAvailability) -> Bool {
        lhs.available == rhs.available
            && lhs.reason == rhs.reason
            && lhs.leaseIdentity == rhs.leaseIdentity
    }
}

struct DotPushApplyOutcome: Equatable {
    let result: DotPushApplyResult
    let availability: DotAvailability

    var acknowledgementStatus: String {
        result.acknowledgementStatus
    }
}

enum DotAnimation: String, CaseIterable, Identifiable {
    case gentle
    case flow
    case kitt
    case tide
    case glow
    case steady

    var id: String { rawValue }

    var label: String {
        switch self {
        case .gentle: return "Gentle"
        case .flow: return "Flow"
        case .kitt: return "KITT"
        case .tide: return "Tide"
        case .glow: return "Glow"
        case .steady: return "Steady"
        }
    }

    var detail: String {
        switch self {
        case .gentle: return "Soft breathing, with a quiet pause."
        case .flow: return "A slow traveling wave."
        case .kitt: return "A relaxed return sweep, about three seconds per cycle."
        case .tide: return "Alternating crossfade, like a slowly turning tide."
        case .glow: return "Softly brightens and dims without going dark."
        case .steady: return "Constant light, without movement."
        }
    }
}

enum DotPalette: String, CaseIterable, Identifiable {
    case ocean, dusk, ice
    var id: String { rawValue }
    var label: String { rawValue.capitalized }
    var colors: [String] {
        switch self {
        case .ocean: return ["#4DA3FF", "#FFB020", "#39D98A"]
        case .dusk: return ["#AC8CFF", "#FFBE70", "#65D99A"]
        case .ice: return ["#65CCFF", "#FFC16E", "#56D99B"]
        }
    }
}

struct DotAppearance: Equatable {
    static let defaultWorkingColor = "#4DA3FF"
    static let defaultNeedsInputColor = "#FFB020"
    static let defaultFinishedColor = "#39D98A"
    static let defaults = DotAppearance()

    var animation: DotAnimation
    var workingColor: String
    var needsInputColor: String
    var finishedColor: String

    init(
        animation: DotAnimation = .gentle,
        workingColor: String? = nil,
        needsInputColor: String? = nil,
        finishedColor: String? = nil
    ) {
        self.animation = animation
        self.workingColor = Self.normalizedHex(
            workingColor,
            fallback: Self.defaultWorkingColor
        )
        self.needsInputColor = Self.normalizedHex(
            needsInputColor,
            fallback: Self.defaultNeedsInputColor
        )
        self.finishedColor = Self.normalizedHex(
            finishedColor,
            fallback: Self.defaultFinishedColor
        )
    }

    var normalized: DotAppearance {
        DotAppearance(
            animation: animation,
            workingColor: workingColor,
            needsInputColor: needsInputColor,
            finishedColor: finishedColor
        )
    }

    var palette: DotPalette? {
        DotPalette.allCases.first { $0.colors == [workingColor, needsInputColor, finishedColor] }
    }

    mutating func apply(_ palette: DotPalette) {
        workingColor = palette.colors[0]
        needsInputColor = palette.colors[1]
        finishedColor = palette.colors[2]
    }

    static func normalizedHex(_ value: String?, fallback: String) -> String {
        let candidate = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let body = candidate.hasPrefix("#") ? String(candidate.dropFirst()) : candidate
        guard body.count == 6, body.allSatisfy(\.isHexDigit) else { return fallback }
        return "#\(body.uppercased())"
    }
}

/// LEDS.LED programs for the 2-LED SidePulse Dot.
enum DotPrograms {
    static let off = "off"

    // Allow several missed 20-minute refresh opportunities, but still settle
    // safely if iOS never delivers the terminal silent push.
    static let workingLifetimeSeconds: TimeInterval = 2 * 60 * 60
    static let workingRefreshSeconds: TimeInterval = 20 * 60
    static let writeFailureRetrySeconds = 5 * 60

    static func program(
        for state: LedDisplayState,
        appearance: DotAppearance,
        finiteWorking: Bool = false,
        showFinished: Bool = false,
        hasUnreadFinished: Bool
    ) -> String {
        let appearance = appearance.normalized
        switch state {
        case .idle:
            return off
        case .ask:
            return appearance.animation == .steady
                ? appearance.needsInputColor : gentleAttention(appearance.needsInputColor)
        case .done:
            return hasUnreadFinished ? appearance.finishedColor : off
        case .working:
            return workingProgram(appearance: appearance, finite: finiteWorking,
                                  finished: showFinished && hasUnreadFinished)
        }
    }

    static func gentleBreath(_ color: String, finite: Bool = false) -> String {
        workingProgram(appearance: DotAppearance(animation: .gentle, workingColor: color), finite: finite)
    }

    static func gentleAttention(_ color: String) -> String {
        "off\n\(color) 2.4s pulse\nrepeat"
    }

    /// `rolling_program(color, led_count=2)`.
    static func rolling(_ color: String, finite: Bool = false) -> String {
        workingProgram(appearance: DotAppearance(animation: .flow, workingColor: color), finite: finite)
    }

    /// `kitt_scanner_program(color, led_count=2)`: scan out, then back.
    static func kittScanner(_ color: String, finite: Bool = false) -> String {
        workingProgram(appearance: DotAppearance(animation: .kitt, workingColor: color), finite: finite)
    }

    /// Keep LED 0 in the finished color while LED 1 continues the selected
    /// working animation. A finite background program preserves that indicator
    /// and turns only the working LED off when its safety window expires.
    static func workingWithFinished(
        appearance: DotAppearance,
        finite: Bool = false
    ) -> String {
        workingProgram(appearance: appearance, finite: finite, finished: true)
    }

    private static func workingProgram(
        appearance: DotAppearance, finite: Bool, finished: Bool = false
    ) -> String {
        let color = appearance.workingColor
        let indexes = finished ? [1] : [0, 1]
        let held = finished ? "0:\(appearance.finishedColor); " : ""
        func frame(_ colors: [String], _ duration: Int, _ easing: String = "cosine") -> String {
            held + zip(indexes, colors).map { "\($0):\($1)" }.joined(separator: " ")
                + " \(duration)ms \(easing)"
        }
        let full = indexes.map { _ in color }
        var lines = [finished ? frame(["#000000"], 400) : "off 400ms cosine"]
        var cycle = 2800
        switch appearance.animation {
        case .steady:
            lines = [frame(full, 60000, "none")]
            cycle = 60000
        case .tide, .glow:
            let rgb = Int(color.dropFirst(), radix: 16) ?? 0
            let low = "#" + [16, 8, 0].map {
                String(format: "%02X", Int((Double((rgb >> $0) & 255) * 0.35).rounded(.toNearestOrEven)))
            }.joined()
            let first = finished || appearance.animation == .glow ? full : [color, low]
            let second = finished || appearance.animation == .glow ? indexes.map { _ in low } : [low, color]
            lines = [frame(second, 400), frame(first, 2200), frame(second, 2200)]
            cycle = 4800
        case .gentle:
            lines.append(frame(full, 2400, "pulse"))
        case .flow, .kitt:
            if finished {
                lines.append(frame(full, 2400, "pulse"))
            } else if appearance.animation == .flow {
                lines.append("0:\(color) 1800ms pulse 0ms; 1:\(color) 1800ms pulse 800ms")
                cycle = 3000
            } else {
                lines += ["0:\(color) 1000ms pulse 0ms; 1:\(color) 1000ms pulse 800ms",
                          "0:\(color) 1000ms pulse 0ms"]
                cycle = 3200
            }
        }
        if finite {
            lines += ["repeat \(Int(ceil(workingLifetimeSeconds * 1000 / Double(cycle))))",
                      held + indexes.map { "\($0):#000000" }.joined(separator: " ")]
        } else {
            lines.append("repeat")
        }
        return lines.joined(separator: "\n")
    }
}

private struct DotWriteSignature: Equatable {
    let program: String
    let brightness: Int
}

private enum DotWriteOutcome {
    case confirmed
    case superseded
    case failed
}

/// Keeps a SidePulse Dot plugged into this phone in step with the Mac's
/// agents, the way the Mac status bar app drives its own Dot: idle → off,
/// working → rolling cyan (or the KITT scanner), done → green, needs input →
/// amber pulse, DND → off. Optionally an active iOS Focus counts as DND too.
///
/// In the foreground the daemon's SSE stream feeds it. iOS only lets the app
/// write to the drive while the process runs, so once the user switches away
/// the daemon sends a silent push whenever the display state changes; iOS
/// wakes the app for a few seconds and `applyPush` writes the Dot, the way
/// the Live Activity is kept current.
@MainActor
final class DotStatusMirror: ObservableObject {
    static let shared = DotStatusMirror()

    /// One stream for both the Dot and the Mac Agents screen.
    let stream = AgentStreamClient()

    @Published private(set) var statusText = "Off"

    private var model: AppModel?
    private var cancellables: Set<AnyCancellable> = []
    private var scheduleTimer: Timer?
    private var lastWriteSignature: DotWriteSignature?
    private var lastSharedWriteID: String?
    private var lastError: String?
    private var lastAttempt: Date = .distantPast
    private var lastProgramWrite: Date = .distantPast
    private var pendingUpdate: Task<Void, Never>?
    private var syncQueued = false
    private var lastAppliedStreamCommandID: String?
    private var streamAcknowledgementsInFlight: Set<String> = []
    private let lastPushCommandIDKey = "lastDotPushCommandID"
    private let lastPushIssuedAtKey = "lastDotPushIssuedAt"
    private let lastStreamUpdatedAtKey = "lastSuccessfulDotStreamUpdatedAt"
    private let streamServerURLKey = "lastSuccessfulDotStreamServerURL"
    private var lastPushSourceUpdatedAt: TimeInterval = 0
    private var lastSuccessfullyAppliedStreamUpdatedAt: TimeInterval = 0
    private var streamServerURL: String?
    /// True while a Focus that shares its status with this app is on. iOS
    /// has no in-app change notification, so it is re-read on every sync
    /// (and by the 15 s timer).
    private var focusActive = false
    private var focusAuthorizationRequested = false
    private var notificationAuthorizationStatus: UNAuthorizationStatus?
    private var notificationAuthorizationCheckInFlight = false
    private var notificationAuthorizationRequestInFlight = false
    /// Matches `AgentLedController.error_retry_seconds` on the Mac.
    private let errorRetrySeconds: TimeInterval = 10
    /// Matches `STATUS_BAR_REFRESH_SECONDS`, which is how often the Mac
    /// checks whether a DND schedule boundary has passed.
    private let scheduleCheckSeconds: TimeInterval = 15
    /// Re-touch an unchanged program periodically while foregrounded so a
    /// removed drive becomes `write_failed` instead of staying ready forever.
    private let connectivityProbeSeconds: TimeInterval = 60
    private let oneHourRetrySeconds = 60 * 60
    private let oneDayRetrySeconds = 24 * 60 * 60

    func start(model: AppModel) {
        guard self.model == nil else { return }
        self.model = model
        notificationAuthorizationStatus = nil
        model.applyDueDndSchedule()
        ensureStreamConnection(model: model)

        // @Published emits on willSet; hop to the next main-queue turn so the
        // sync reads the new values.
        let triggers: [AnyPublisher<Void, Never>] = [
            stream.$snapshot.map { _ in () }.eraseToAnyPublisher(),
            stream.$state.map { _ in () }.eraseToAnyPublisher(),
            model.$dotBrightness
                .debounce(for: .milliseconds(150), scheduler: DispatchQueue.main)
                .map { _ in () }
                .eraseToAnyPublisher(),
            model.$dotAppearance
                .debounce(for: .milliseconds(150), scheduler: DispatchQueue.main)
                .map { _ in () }
                .eraseToAnyPublisher(),
            model.$showFinishedEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$dndEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$dndScheduleEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$dndStartTime.map { _ in () }.eraseToAnyPublisher(),
            model.$dndEndTime.map { _ in () }.eraseToAnyPublisher(),
            model.$focusDndEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$hasFolderAccess.map { _ in () }.eraseToAnyPublisher(),
            model.$pushToken.map { _ in () }.eraseToAnyPublisher(),
            model.$liveMonitorServerURL.map { _ in () }.eraseToAnyPublisher(),
        ]
        Publishers.MergeMany(triggers)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                MainActor.assumeIsolated { self?.sync() }
            }
            .store(in: &cancellables)

        scheduleTimer = Timer.scheduledTimer(withTimeInterval: scheduleCheckSeconds, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, let model = self.model else { return }
                model.applyDueDndSchedule()
                self.sync()
            }
        }
        sync()
    }

    /// Every working program is finite, including foreground writes. Going
    /// into the background no longer needs one last USB write before suspension.
    func suspend() {
        guard model != nil else { return }
        EventLog.append("Dot mirror backgrounded; finite program remains on the device")
        cancellables.removeAll()
        scheduleTimer?.invalidate()
        scheduleTimer = nil
        stream.stop()
        lastWriteSignature = nil
        focusActive = false
        notificationAuthorizationStatus = nil
        model = nil
    }

    /// Silent push from the daemon (`dot` payload). The app may have been
    /// launched for it with no scene, so everything needed is read here.
    /// Every push leaves a line in the diagnostics log, since a background
    /// wake is otherwise invisible. Returns the result used by the daemon's
    /// write acknowledgement protocol.
    @discardableResult
    func applyPush(
        aggregateMode: String,
        hasUnreadFinished: Bool = false,
        commandID: String? = nil,
        issuedAt: TimeInterval? = nil,
        sourceUpdatedAt: TimeInterval? = nil,
        host: String? = nil,
        model: AppModel
    ) async -> DotPushApplyOutcome {
        await enqueueUpdate {
            await self.applyPushNow(
                aggregateMode: aggregateMode,
                hasUnreadFinished: hasUnreadFinished,
                commandID: commandID,
                issuedAt: issuedAt,
                sourceUpdatedAt: sourceUpdatedAt,
                host: host,
                model: model
            )
        }.value
    }

    /// Serialize decisions as well as USB I/O, so a delayed working write
    /// cannot finish after a newer completed/off write and be acknowledged.
    @discardableResult
    private func enqueueUpdate<T>(_ operation: @escaping @MainActor () async -> T) -> Task<T, Never> {
        let previous = pendingUpdate
        let task = Task { @MainActor in
            await previous?.value
            return await operation()
        }
        pendingUpdate = Task { _ = await task.value }
        return task
    }

    private func applyPushNow(
        aggregateMode: String,
        hasUnreadFinished: Bool,
        commandID: String?,
        issuedAt: TimeInterval?,
        sourceUpdatedAt: TimeInterval?,
        host: String?,
        model: AppModel
    ) async -> DotPushApplyOutcome {
        defer { model.refreshEventLog() }
        configureStreamScope(serverURL: model.liveMonitorServerURL)
        let now = Date()
        model.applyDueDndSchedule(now: now)
        // This must happen before command deduplication: a retry can be the
        // first wake after a DND boundary or Focus change.
        refreshFocusStatus(model: model, allowPrompt: false)
        let scope = host ?? "unknown"
        let commandKey = "\(lastPushCommandIDKey).\(scope)"
        let issuedKey = "\(lastPushIssuedAtKey).\(scope)"
        let defaults = UserDefaults.standard
        let appliedAt = defaults.double(forKey: issuedKey)
        let appliedID = defaults.string(forKey: commandKey)
        var staleCommand = false
        var duplicateCommand = false
        if let commandID, let issuedAt {
            staleCommand = issuedAt < appliedAt
            duplicateCommand = issuedAt == appliedAt && commandID == appliedID
        }
        if let sourceUpdatedAt,
           latestSourceUpdatedAt(serverURL: model.liveMonitorServerURL) > sourceUpdatedAt {
            staleCommand = true
        }

        guard model.hasFolderAccess else {
            EventLog.append("Dot push (\(aggregateMode)): no Dot folder selected")
            return DotPushApplyOutcome(
                result: .noFolder,
                availability: .unavailable(
                    reason: "no_folder",
                    retryAfterSeconds: oneDayRetrySeconds
                )
            )
        }

        LiveMonitorManager.shared.ensureDotDeviceRegistration(model: model)
        var suppression = configuredUnavailability(model: model, now: now)
        var currentMode = aggregateMode
        var currentUnread = hasUnreadFinished
        var currentSourceUpdatedAt = sourceUpdatedAt
        // Never let an old agent state replace a newer foreground write. A
        // suppression still has to be applied, because the duplicate wake
        // may be the first one after DND or Focus switched on.
        if staleCommand, suppression == nil {
            // A timestamp is not proof that a finite program is still running.
            // Fetch current state before refreshing; never replay an old mode.
            let serverURL = model.liveMonitorServerURL
            do {
                guard let url = URL(string: serverURL)?.appendingPathComponent("snapshot") else {
                    throw URLError(.badURL)
                }
                let (data, response) = try await URLSession.shared.data(for: URLRequest(
                    url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5
                ))
                guard (response as? HTTPURLResponse)?.statusCode == 200,
                      serverURL == model.liveMonitorServerURL else { throw URLError(.badServerResponse) }
                let snapshot = try JSONDecoder().decode(AgentSnapshot.self, from: data)
                guard snapshot.updatedAt >= latestSourceUpdatedAt(serverURL: serverURL) else {
                    throw URLError(.badServerResponse)
                }
                currentMode = snapshot.aggregateMode
                currentUnread = snapshotHasUnreadFinished(snapshot)
                currentSourceUpdatedAt = snapshot.updatedAt
                model.applyDueDndSchedule()
                refreshFocusStatus(model: model, allowPrompt: false)
                suppression = configuredUnavailability(model: model, now: Date())
                EventLog.append("Dot stale push: fetched current state before USB refresh")
            } catch {
                EventLog.append("Dot stale push: current state unavailable; not acknowledged")
                return DotPushApplyOutcome(result: .failed, availability: availabilityAfterWrite(lastError == nil, model: model, now: Date()))
            }
        }

        let resolved = resolve(mode: currentMode, unreachable: false, model: model)
        let label = displayLabel(
            state: resolved.state,
            fallback: resolved.label,
            hasUnreadFinished: currentUnread
        )
        let program = DotPrograms.program(
            for: resolved.state,
            appearance: model.dotAppearance,
            finiteWorking: resolved.state == .working,
            showFinished: model.showFinishedEnabled,
            hasUnreadFinished: currentUnread
        )
        let signature = DotWriteSignature(
            program: program,
            brightness: DotBrightness.configuredValue
        )
        let refreshWorking = resolved.state == .working
        let alreadyCurrent = !refreshWorking
            && signature == lastWriteSignature
            && lastError == nil
        let outcome = await write(
            program,
            label: label,
            force: refreshWorking,
            serverURL: model.liveMonitorServerURL,
            sourceUpdatedAt: suppression == nil ? currentSourceUpdatedAt : nil
        )
        if outcome == .confirmed {
            if !staleCommand {
                recordPushCommand(commandID: commandID, issuedAt: issuedAt, scope: scope)
            }
            if let currentSourceUpdatedAt {
                lastPushSourceUpdatedAt = max(lastPushSourceUpdatedAt, currentSourceUpdatedAt)
            }
            let suffix = duplicateCommand && alreadyCurrent ? "already current" : label
            EventLog.append("Dot push (\(aggregateMode)): \(suffix); unread finished: \(currentUnread), show finished: \(model.showFinishedEnabled)")
            return DotPushApplyOutcome(
                result: alreadyCurrent ? .alreadyCurrent : .written,
                availability: suppression ?? .ready
            )
        } else {
            if outcome == .superseded {
                EventLog.append("Dot push (\(aggregateMode)): superseded during USB coordination; not acknowledged")
            } else {
                EventLog.append("Dot push (\(aggregateMode)) failed: \(lastError ?? "unknown error")")
            }
            return DotPushApplyOutcome(
                result: .failed,
                availability: suppression ?? availabilityAfterWrite(
                    outcome == .superseded, model: model, now: Date()
                )
            )
        }
    }

    /// Focus status needs the user's one-time consent (system prompt) and, per
    /// Focus, "Share Focus Status" enabled in iOS Settings; anything else
    /// reads as "no Focus".
    private func refreshFocusStatus(model: AppModel, allowPrompt: Bool) {
        guard model.focusDndEnabled else {
            focusActive = false
            notificationAuthorizationStatus = nil
            return
        }
        refreshNotificationAuthorization(allowPrompt: allowPrompt)
        if allowPrompt,
           notificationAuthorizationStatus == nil
                || notificationAuthorizationStatus == .notDetermined
        {
            focusActive = false
            return
        }
        let center = INFocusStatusCenter.default
        switch center.authorizationStatus {
        case .authorized:
            focusActive = center.focusStatus.isFocused ?? false
        case .notDetermined:
            focusActive = false
            guard allowPrompt, !focusAuthorizationRequested else { return }
            focusAuthorizationRequested = true
            center.requestAuthorization { [weak self] _ in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated { self?.sync() }
                }
            }
        default:
            focusActive = false
        }
    }

    /// Apple only delivers Focus status updates to the intent extension when
    /// the containing app also has notification authorization. Dot-only users
    /// may never enable Live Monitor, so request that permission here too.
    private func refreshNotificationAuthorization(allowPrompt: Bool) {
        guard allowPrompt,
              notificationAuthorizationStatus == nil,
              !notificationAuthorizationCheckInFlight,
              !notificationAuthorizationRequestInFlight
        else { return }

        notificationAuthorizationCheckInFlight = true
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { [weak self] settings in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.notificationAuthorizationCheckInFlight = false
                    self.notificationAuthorizationStatus = settings.authorizationStatus
                    guard settings.authorizationStatus == .notDetermined else {
                        self.sync()
                        return
                    }

                    self.notificationAuthorizationRequestInFlight = true
                    center.requestAuthorization(options: [.alert, .badge, .sound]) { [weak self] _, _ in
                        center.getNotificationSettings { updatedSettings in
                            DispatchQueue.main.async {
                                MainActor.assumeIsolated {
                                    guard let self else { return }
                                    self.notificationAuthorizationRequestInFlight = false
                                    self.notificationAuthorizationStatus = updatedSettings.authorizationStatus
                                    self.sync()
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private var focusAccessHint: String? {
        guard let model, model.focusDndEnabled else { return nil }
        if notificationAuthorizationStatus == .denied {
            return " · Notifications denied — Focus updates unavailable"
        }
        switch INFocusStatusCenter.default.authorizationStatus {
        case .denied, .restricted:
            return " · Focus access denied (iOS Settings › SidePulse)"
        default:
            return nil
        }
    }

    private func sync() {
        guard !syncQueued else { return }
        syncQueued = true
        enqueueUpdate {
            self.syncQueued = false
            await self.syncNow()
        }
    }

    private func syncNow() async {
        guard let model else { return }
        ensureStreamConnection(model: model)
        let now = Date()
        model.applyDueDndSchedule(now: now)
        // Before the folder check so switching the option on asks for Focus
        // access right away.
        refreshFocusStatus(model: model, allowPrompt: true)
        guard model.hasFolderAccess else {
            lastWriteSignature = nil
            statusText = "No SidePulse Dot folder selected"
            reportAvailability(
                .unavailable(reason: "no_folder", retryAfterSeconds: oneDayRetrySeconds),
                model: model
            )
            return
        }

        LiveMonitorManager.shared.ensureDotDeviceRegistration(model: model)

        if let suppression = configuredUnavailability(model: model, now: now) {
            let label: String
            switch suppression.reason {
            case "brightness_zero": label = "Brightness is 0 — Dot off"
            case "dnd": label = "DND on — Dot off"
            case "focus": label = "iOS Focus on — Dot off"
            default: label = "Dot unavailable"
            }
            let outcome = await write(
                DotPrograms.off, label: label, serverURL: model.liveMonitorServerURL
            )
            reportAvailability(
                availabilityAfterWrite(outcome != .failed, model: model, now: now),
                model: model
            )
            return
        }

        let mode: String?
        let unreachable: Bool
        var hasUnreadFinished = false
        var streamUpdatedAt: TimeInterval?
        var streamCommandID: String?
        switch stream.state {
        case .live:
            guard let snapshot = stream.snapshot else { return }
            guard snapshot.updatedAt >= latestSourceUpdatedAt(serverURL: model.liveMonitorServerURL) else {
                statusText = "Waiting for current Mac state"
                return
            }
            mode = snapshot.aggregateMode
            hasUnreadFinished = snapshot.agents.contains {
                $0.mode == "completed" && $0.unread == true
            }
            streamUpdatedAt = snapshot.updatedAt
            streamCommandID = snapshot.dotCommandID
            unreachable = false
        case .failed:
            mode = nil
            unreachable = true
        case .idle, .connecting:
            statusText = "Connecting to Mac…"
            return
        }
        let resolved = resolve(mode: mode, unreachable: unreachable, model: model)
        var label = displayLabel(
            state: resolved.state,
            fallback: resolved.label,
            hasUnreadFinished: hasUnreadFinished
        )
        if let focusAccessHint {
            label += focusAccessHint
        }
        let previousProgramWrite = lastProgramWrite
        let outcome = await write(
            DotPrograms.program(
                for: resolved.state,
                appearance: model.dotAppearance,
                finiteWorking: resolved.state == .working,
                showFinished: model.showFinishedEnabled,
                hasUnreadFinished: hasUnreadFinished
            ),
            label: label,
            force: resolved.state == .working
                && lastError == nil
                && (now.timeIntervalSince(lastProgramWrite) >= DotPrograms.workingRefreshSeconds
                    || (streamCommandID != nil && streamCommandID != lastAppliedStreamCommandID)),
            serverURL: model.liveMonitorServerURL,
            sourceUpdatedAt: streamUpdatedAt
        )
        let written = outcome == .confirmed
        if written, let streamUpdatedAt {
            recordSuccessfulStreamWrite(updatedAt: streamUpdatedAt)
        }
        if written, let commandID = streamCommandID,
           !streamAcknowledgementsInFlight.contains(commandID) {
            lastAppliedStreamCommandID = commandID
            streamAcknowledgementsInFlight.insert(commandID)
            let status = previousProgramWrite == lastProgramWrite ? "alreadyCurrent" : "written"
            let serverURL = model.liveMonitorServerURL
            EventLog.append("Dot foreground receipt \(commandID.prefix(8)): \(label); unread finished: \(hasUnreadFinished)")
            // A slow HTTP acknowledgement must not hold up the next USB write.
            Task { @MainActor in
                defer { self.streamAcknowledgementsInFlight.remove(commandID) }
                guard model.liveMonitorServerURL == serverURL else { return }
                model.applyDueDndSchedule()
                self.refreshFocusStatus(model: model, allowPrompt: false)
                let availability = self.availabilityAfterWrite(true, model: model, now: Date())
                guard availability.available else {
                    self.reportAvailability(availability, model: model)
                    return
                }
                await LiveMonitorManager.shared.acknowledgeDot(
                    commandID: commandID,
                    status: status,
                    availability: availability,
                    model: model
                )
            }
        }
        reportAvailability(
            availabilityAfterWrite(outcome != .failed, model: model, now: now),
            model: model
        )
    }

    private func latestSourceUpdatedAt(serverURL: String) -> TimeInterval {
        max(
            max(lastSuccessfullyAppliedStreamUpdatedAt, lastPushSourceUpdatedAt),
            DotNotificationShared.latestWrite(serverURL: serverURL)?.sourceUpdatedAt ?? 0
        )
    }

    private func recordPushCommand(
        commandID: String?,
        issuedAt: TimeInterval?,
        scope: String
    ) {
        guard let commandID, let issuedAt else { return }
        let defaults = UserDefaults.standard
        defaults.set(commandID, forKey: "\(lastPushCommandIDKey).\(scope)")
        defaults.set(issuedAt, forKey: "\(lastPushIssuedAtKey).\(scope)")
    }

    private func recordSuccessfulStreamWrite(updatedAt: TimeInterval) {
        lastSuccessfullyAppliedStreamUpdatedAt = max(
            lastSuccessfullyAppliedStreamUpdatedAt,
            updatedAt
        )
        let defaults = UserDefaults.standard
        defaults.set(lastSuccessfullyAppliedStreamUpdatedAt, forKey: lastStreamUpdatedAtKey)
        if updatedAt >= lastPushSourceUpdatedAt {
            lastPushSourceUpdatedAt = 0
        }
    }

    private func configureStreamScope(serverURL: String) {
        guard streamServerURL != serverURL else { return }
        let defaults = UserDefaults.standard
        let storedURL = defaults.string(forKey: streamServerURLKey)
        lastPushSourceUpdatedAt = 0
        lastAppliedStreamCommandID = nil
        lastSuccessfullyAppliedStreamUpdatedAt = storedURL == serverURL
            ? defaults.double(forKey: lastStreamUpdatedAtKey)
            : 0
        if storedURL != serverURL {
            defaults.set(serverURL, forKey: streamServerURLKey)
            defaults.removeObject(forKey: lastStreamUpdatedAtKey)
        }
        streamServerURL = serverURL
    }

    private func ensureStreamConnection(model: AppModel) {
        configureStreamScope(serverURL: model.liveMonitorServerURL)
        let dotToken = model.hasFolderAccess && !model.pushToken.isEmpty
            ? model.pushToken
            : nil
        stream.start(baseURL: model.liveMonitorServerURL, dotToken: dotToken)
    }

    private func snapshotHasUnreadFinished(_ snapshot: AgentSnapshot) -> Bool {
        snapshot.agents.contains {
            $0.mode == "completed" && $0.unread == true
        }
    }

    private func displayLabel(
        state: LedDisplayState,
        fallback: String,
        hasUnreadFinished: Bool
    ) -> String {
        state == .done && !hasUnreadFinished ? "All read — Dot off" : fallback
    }

    private func configuredUnavailability(model: AppModel, now: Date) -> DotAvailability? {
        guard model.hasFolderAccess else {
            return .unavailable(reason: "no_folder", retryAfterSeconds: oneDayRetrySeconds)
        }
        if model.dotBrightness == 0 {
            return .unavailable(reason: "brightness_zero", retryAfterSeconds: oneDayRetrySeconds)
        }
        if model.dndEnabled {
            if model.dndScheduleEnabled,
               DndSchedule.latestTransition(
                   startTime: model.dndStartTime,
                   endTime: model.dndEndTime,
                   now: now
               )?.enabled == true,
               let boundary = DndSchedule.nextTransitionDate(
                   startTime: model.dndStartTime,
                   endTime: model.dndEndTime,
                   after: now
               )
            {
                let retry = max(1, Int(ceil(boundary.timeIntervalSince(now))))
                return .unavailable(
                    reason: "dnd",
                    retryAfterSeconds: retry,
                    leaseIdentity: "dnd:\(Int(boundary.timeIntervalSince1970))"
                )
            }
            return .unavailable(reason: "dnd", retryAfterSeconds: oneDayRetrySeconds)
        }
        if focusActive {
            return .unavailable(reason: "focus", retryAfterSeconds: oneHourRetrySeconds)
        }
        return nil
    }

    private func availabilityAfterWrite(
        _ succeeded: Bool,
        model: AppModel,
        now: Date
    ) -> DotAvailability {
        if let suppression = configuredUnavailability(model: model, now: now) { return suppression }
        guard succeeded else {
            return .unavailable(reason: "write_failed", retryAfterSeconds: DotPrograms.writeFailureRetrySeconds)
        }
        return .ready
    }

    private func reportAvailability(_ availability: DotAvailability, model: AppModel) {
        LiveMonitorManager.shared.reportDotAvailability(availability, model: model)
    }

    private func resolve(mode: String?, unreachable: Bool, model: AppModel) -> (state: LedDisplayState, label: String) {
        if model.dndEnabled {
            return (.idle, "DND on — Dot off")
        }
        if focusActive {
            return (.idle, "iOS Focus on — Dot off")
        }
        if unreachable {
            return (.idle, "Mac unreachable — Dot off")
        }
        let state = mode.map(LedDisplayState.forMode) ?? .idle
        return (
            state,
            state == .working && model.dotAppearance.animation == .kitt
                ? "Working (KITT)"
                : state.label
        )
    }

    static func sharedWriteInvalidatesCache(
        _ receipt: DotWriteReceipt,
        lastWriteID: String?,
        lastProgramWrite: Date
    ) -> Bool {
        if let writeID = receipt.writeID { return writeID != lastWriteID }
        return receipt.completedAt > lastProgramWrite.timeIntervalSince1970
    }

    /// Writes only when the program or configured brightness changes, or when
    /// retrying a failed write after the back-off. Success confirms file I/O,
    /// not physical LED feedback from the firmware.
    @discardableResult
    private func write(
        _ program: String,
        label: String,
        force: Bool = false,
        serverURL: String,
        sourceUpdatedAt: TimeInterval? = nil
    ) async -> DotWriteOutcome {
        // An extension write may finish before this process resumes from its
        // own write, so compare write identities, not async completion times.
        if let receipt = DotNotificationShared.latestWrite(serverURL: serverURL),
           Self.sharedWriteInvalidatesCache(
               receipt, lastWriteID: lastSharedWriteID, lastProgramWrite: lastProgramWrite
           ) {
            lastWriteSignature = nil
        }
        let signature = DotWriteSignature(
            program: program,
            brightness: DotBrightness.configuredValue
        )
        let now = Date()
        if !force, signature == lastWriteSignature {
            if lastError == nil {
                if now.timeIntervalSince(lastAttempt) < connectivityProbeSeconds {
                    statusText = label
                    return .confirmed
                }
                lastAttempt = now
                do {
                    try await DriveWriter.shared.probeAccess()
                    statusText = label
                    return .confirmed
                } catch {
                    lastError = error.localizedDescription
                    statusText = "Dot access failed: \(error.localizedDescription)"
                    return .failed
                }
            }
            if lastError != nil,
               now.timeIntervalSince(lastAttempt) < errorRetrySeconds {
                return .failed
            }
        }

        lastAttempt = now
        lastWriteSignature = signature
        let context = DotWriteContext(serverURL: serverURL, sourceUpdatedAt: sourceUpdatedAt)
        do {
            try await DriveWriter.shared.write(program, context: context)
            lastError = nil
            lastSharedWriteID = context.writeID
            lastProgramWrite = Date()
            if program.contains("repeat ") {
                EventLog.append("Dot finite working program refreshed; safety window: 120 minutes")
            }
            statusText = label
            return .confirmed
        } catch DotNotificationError.superseded {
            // Another process wrote newer state after this update was queued.
            // Skip the old command without treating healthy USB access as broken.
            lastWriteSignature = nil
            lastSharedWriteID = nil
            lastError = nil
            lastAttempt = .distantPast
            statusText = "Waiting for current Mac state"
            return .superseded
        } catch {
            lastError = error.localizedDescription
            statusText = "Dot write failed: \(error.localizedDescription)"
            return .failed
        }
    }
}
