import Combine
import Foundation
import Intents

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

/// LEDS.LED programs for the 2-LED SidePulse Dot — the same text the Mac app
/// writes (`program_for_display_state` with `led_count=2`), so the Dot looks
/// identical whichever machine it is plugged into.
enum DotPrograms {
    static let off = "off"
    static let askAmber = "#FF3A00"
    static let workingCyan = "#00E5FF"
    static let workingHoldCyan = "#002D33"
    static let doneGreen = "#00FF66"

    static func program(
        for state: LedDisplayState,
        kittMode: Bool,
        finiteWorking: Bool = false,
        showFinished: Bool = false
    ) -> String {
        switch state {
        case .idle:
            return off
        case .ask:
            return "off\n\(askAmber) 1.6s pulse\nrepeat"
        case .done:
            return doneGreen
        case .working:
            if showFinished {
                return workingWithFinished(kittMode: kittMode, finite: finiteWorking)
            }
            return kittMode
                ? kittScanner(workingCyan, finite: finiteWorking)
                : rolling(workingCyan, finite: finiteWorking)
        }
    }

    /// `rolling_program(color, led_count=2)`.
    static func rolling(_ color: String, finite: Bool = false) -> String {
        let ending = finite ? "repeat 100\n\(workingHoldCyan)" : "repeat"
        return "off 160ms cosine\n0:\(color) 760ms pulse 0ms; 1:\(color) 760ms pulse 260ms\n\(ending)"
    }

    /// `kitt_scanner_program(color, led_count=2)`: scan out, then back.
    static func kittScanner(_ color: String, finite: Bool = false) -> String {
        let ending = finite ? "repeat 125\n\(workingHoldCyan)" : "repeat"
        return "off 80ms cosine\n0:\(color) 320ms pulse 0ms; 1:\(color) 320ms pulse 240ms\n0:\(color) 320ms pulse 0ms\n\(ending)"
    }

    /// Keep LED 0 solid green while LED 1 continues the selected working
    /// animation. A finite background program preserves the green indicator
    /// and lets only the working LED settle to dim cyan.
    static func workingWithFinished(kittMode: Bool, finite: Bool = false) -> String {
        let resetDuration = kittMode ? "80ms" : "160ms"
        let pulseDuration = kittMode ? "320ms" : "760ms"
        let repeatCount = kittMode ? 125 : 100
        let ending = finite
            ? "repeat \(repeatCount)\n0:\(doneGreen); 1:\(workingHoldCyan)"
            : "repeat"
        return "0:\(doneGreen); 1:#000000 \(resetDuration) cosine\n1:\(workingCyan) \(pulseDuration) pulse 0ms\n\(ending)"
    }
}

struct DndScheduleTransition: Equatable {
    let key: String
    let enabled: Bool
}

private struct DotWriteSignature: Equatable {
    let program: String
    let brightness: Int
}

/// Daily DND window, ported from `status_bar.py`. Times are local "HH:MM".
enum DndSchedule {
    static let defaultStartTime = "22:00"
    static let defaultEndTime = "07:00"

    /// The most recent start/end boundary at or before `now`, looking back
    /// one day so a window that opened yesterday evening is still honoured
    /// this morning. Its key lets the caller apply each boundary exactly once.
    static func latestTransition(startTime: String, endTime: String, now: Date = Date()) -> DndScheduleTransition? {
        guard let start = parse(startTime), let end = parse(endTime) else { return nil }
        var boundaries = [(label: "start", time: start, enabled: true)]
        if start != end {
            boundaries.append((label: "end", time: end, enabled: false))
        }

        let calendar = Calendar.current
        var due: [(date: Date, label: String, enabled: Bool)] = []
        for dayOffset in [-1, 0] {
            guard let day = calendar.date(byAdding: .day, value: dayOffset, to: now) else { continue }
            var components = calendar.dateComponents([.year, .month, .day], from: day)
            for boundary in boundaries {
                components.hour = boundary.time.hour
                components.minute = boundary.time.minute
                components.second = 0
                guard let date = calendar.date(from: components), date <= now else { continue }
                due.append((date, boundary.label, boundary.enabled))
            }
        }
        guard let latest = due.max(by: { $0.date < $1.date }) else { return nil }

        let day = calendar.dateComponents([.year, .month, .day, .hour, .minute], from: latest.date)
        let key = String(
            format: "%04d-%02d-%02d:%@:%02d:%02d",
            day.year ?? 0, day.month ?? 0, day.day ?? 0, latest.label, day.hour ?? 0, day.minute ?? 0
        )
        return DndScheduleTransition(key: key, enabled: latest.enabled)
    }

    static func nextTransitionDate(startTime: String, endTime: String, after now: Date = Date()) -> Date? {
        guard let start = parse(startTime), let end = parse(endTime) else { return nil }
        var times = [start]
        if start != end {
            times.append(end)
        }

        let calendar = Calendar.current
        var candidates: [Date] = []
        for dayOffset in 0...2 {
            guard let day = calendar.date(byAdding: .day, value: dayOffset, to: now) else { continue }
            var components = calendar.dateComponents([.year, .month, .day], from: day)
            for time in times {
                components.hour = time.hour
                components.minute = time.minute
                components.second = 0
                if let date = calendar.date(from: components), date > now {
                    candidates.append(date)
                }
            }
        }
        return candidates.min()
    }

    static func parse(_ value: String) -> (hour: Int, minute: Int)? {
        let parts = value.split(separator: ":", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
        guard parts.count == 2,
              let hour = Int(parts[0]), let minute = Int(parts[1]),
              (0...23).contains(hour), (0...59).contains(minute)
        else { return nil }
        return (hour, minute)
    }

    static func format(hour: Int, minute: Int) -> String {
        String(format: "%02d:%02d", hour, minute)
    }
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
    private var lastError: String?
    private var hasSuccessfulWrite = false
    private var lastAttempt: Date = .distantPast
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
        configureStreamScope(serverURL: model.liveMonitorServerURL)
        model.applyDueDndSchedule()
        stream.start(baseURL: model.liveMonitorServerURL)

        // @Published emits on willSet; hop to the next main-queue turn so the
        // sync reads the new values.
        let triggers: [AnyPublisher<Void, Never>] = [
            stream.$snapshot.map { _ in () }.eraseToAnyPublisher(),
            stream.$state.map { _ in () }.eraseToAnyPublisher(),
            model.$dotBrightness
                .debounce(for: .milliseconds(150), scheduler: DispatchQueue.main)
                .map { _ in () }
                .eraseToAnyPublisher(),
            model.$kittModeEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$showFinishedEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$dndEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$dndScheduleEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$dndStartTime.map { _ in () }.eraseToAnyPublisher(),
            model.$dndEndTime.map { _ in () }.eraseToAnyPublisher(),
            model.$focusDndEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$hasFolderAccess.map { _ in () }.eraseToAnyPublisher(),
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

    /// Scene-background: drop the stream and timer. The Dot keeps its last
    /// state; from here on the Mac's pushes update it. Replace a foreground
    /// infinite working animation with a finite one first, so a suppressed
    /// terminal push can never leave the Dot blinking forever.
    func suspend() {
        guard model != nil else { return }
        if let model,
           model.hasFolderAccess,
           case .live = stream.state,
           let snapshot = stream.snapshot
        {
            model.applyDueDndSchedule()
            refreshFocusStatus(model: model, allowPrompt: false)
            let resolved = resolve(
                mode: snapshot.aggregateMode,
                unreachable: false,
                model: model
            )
            let written = write(
                DotPrograms.program(
                    for: resolved.state,
                    kittMode: model.kittModeEnabled,
                    finiteWorking: resolved.state == .working,
                    showFinished: shouldShowFinished(snapshot, model: model)
                ),
                label: resolved.label
            )
            if written {
                recordSuccessfulStreamWrite(updatedAt: snapshot.updatedAt)
            }
            reportAvailability(
                availabilityAfterWrite(written, model: model, now: Date()),
                model: model
            )
        }
        cancellables.removeAll()
        scheduleTimer?.invalidate()
        scheduleTimer = nil
        stream.stop()
        lastWriteSignature = nil
        lastError = nil
        focusActive = false
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
    ) -> DotPushApplyOutcome {
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
           lastSuccessfullyAppliedStreamUpdatedAt > sourceUpdatedAt {
            staleCommand = true
        }

        guard model.hasFolderAccess else {
            hasSuccessfulWrite = false
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
        let suppression = configuredUnavailability(model: model, now: now)
        // Never let an old agent state replace a newer foreground write. A
        // suppression still has to be applied, because the duplicate wake
        // may be the first one after DND or Focus switched on.
        if staleCommand, suppression == nil {
            let persistedSuccess = appliedAt > 0 && appliedID != nil
            let newerStreamSuccess = sourceUpdatedAt.map {
                lastSuccessfullyAppliedStreamUpdatedAt > $0
            } ?? false
            let currentWriteProven = lastError == nil
                && (hasSuccessfulWrite || persistedSuccess || newerStreamSuccess)
            EventLog.append("Dot push (\(aggregateMode)): newer state already applied")
            return DotPushApplyOutcome(
                result: .alreadyCurrent,
                availability: currentWriteProven
                    ? .ready
                    : .unavailable(
                        reason: "write_failed",
                        retryAfterSeconds: oneHourRetrySeconds
                    )
            )
        }

        let resolved = resolve(mode: aggregateMode, unreachable: false, model: model)
        let program = DotPrograms.program(
            for: resolved.state,
            kittMode: model.kittModeEnabled,
            finiteWorking: resolved.state == .working,
            showFinished: model.showFinishedEnabled && hasUnreadFinished
        )
        let signature = DotWriteSignature(
            program: program,
            brightness: DotBrightness.configuredValue
        )
        let alreadyCurrent = signature == lastWriteSignature && lastError == nil
        let written = write(
            program,
            label: resolved.label
        )
        if written {
            if !staleCommand {
                recordPushCommand(commandID: commandID, issuedAt: issuedAt, scope: scope)
            }
            if !staleCommand, let sourceUpdatedAt {
                lastPushSourceUpdatedAt = max(lastPushSourceUpdatedAt, sourceUpdatedAt)
            }
            let suffix = duplicateCommand && alreadyCurrent ? "already current" : resolved.label
            EventLog.append("Dot push (\(aggregateMode)): \(suffix)")
            return DotPushApplyOutcome(
                result: alreadyCurrent ? .alreadyCurrent : .written,
                availability: suppression ?? .ready
            )
        } else {
            EventLog.append("Dot push (\(aggregateMode)) failed: \(lastError ?? "unknown error")")
            return DotPushApplyOutcome(
                result: .failed,
                availability: .unavailable(
                    reason: "write_failed",
                    retryAfterSeconds: oneHourRetrySeconds
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

    private var focusAccessHint: String? {
        guard let model, model.focusDndEnabled else { return nil }
        switch INFocusStatusCenter.default.authorizationStatus {
        case .denied, .restricted:
            return " · Focus access denied (iOS Settings › SidePulse)"
        default:
            return nil
        }
    }

    private func sync() {
        guard let model else { return }
        let now = Date()
        model.applyDueDndSchedule(now: now)
        // Before the folder check so switching the option on asks for Focus
        // access right away.
        refreshFocusStatus(model: model, allowPrompt: true)
        guard model.hasFolderAccess else {
            lastWriteSignature = nil
            hasSuccessfulWrite = false
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
            let written = write(DotPrograms.off, label: label)
            reportAvailability(
                availabilityAfterWrite(written, model: model, now: now),
                model: model
            )
            return
        }

        let mode: String?
        let unreachable: Bool
        var hasUnreadFinished = false
        var streamUpdatedAt: TimeInterval?
        switch stream.state {
        case .live:
            guard let snapshot = stream.snapshot else { return }
            guard snapshot.updatedAt >= lastPushSourceUpdatedAt else {
                statusText = "Waiting for current Mac state"
                return
            }
            mode = snapshot.aggregateMode
            hasUnreadFinished = snapshot.agents.contains {
                $0.mode == "completed" && $0.unread == true
            }
            streamUpdatedAt = snapshot.updatedAt
            unreachable = false
        case .failed:
            mode = nil
            unreachable = true
        case .idle, .connecting:
            statusText = "Connecting to Mac…"
            return
        }
        let resolved = resolve(mode: mode, unreachable: unreachable, model: model)
        var label = resolved.label
        if let focusAccessHint {
            label += focusAccessHint
        }
        let written = write(
            DotPrograms.program(
                for: resolved.state,
                kittMode: model.kittModeEnabled,
                showFinished: model.showFinishedEnabled && hasUnreadFinished
            ),
            label: label
        )
        if written, let streamUpdatedAt {
            recordSuccessfulStreamWrite(updatedAt: streamUpdatedAt)
        }
        reportAvailability(
            availabilityAfterWrite(written, model: model, now: now),
            model: model
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
        lastSuccessfullyAppliedStreamUpdatedAt = storedURL == serverURL
            ? defaults.double(forKey: lastStreamUpdatedAtKey)
            : 0
        if storedURL != serverURL {
            defaults.set(serverURL, forKey: streamServerURLKey)
            defaults.removeObject(forKey: lastStreamUpdatedAtKey)
        }
        streamServerURL = serverURL
    }

    private func shouldShowFinished(_ snapshot: AgentSnapshot, model: AppModel) -> Bool {
        model.showFinishedEnabled && snapshot.agents.contains {
            $0.mode == "completed" && $0.unread == true
        }
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
        guard succeeded else {
            return .unavailable(reason: "write_failed", retryAfterSeconds: oneHourRetrySeconds)
        }
        return configuredUnavailability(model: model, now: now) ?? .ready
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
        return (state, state == .working && model.kittModeEnabled ? "Working (KITT)" : state.label)
    }

    /// Writes only when the program or configured brightness changes, or when
    /// retrying a failed write after the back-off. Returns true when the Dot
    /// shows `program`.
    @discardableResult
    private func write(_ program: String, label: String) -> Bool {
        let signature = DotWriteSignature(
            program: program,
            brightness: DotBrightness.configuredValue
        )
        let now = Date()
        if signature == lastWriteSignature {
            if lastError == nil {
                if now.timeIntervalSince(lastAttempt) < connectivityProbeSeconds {
                    statusText = label
                    return true
                }
                lastAttempt = now
                do {
                    try DriveWriter.shared.probeAccess()
                    hasSuccessfulWrite = true
                    statusText = label
                    return true
                } catch {
                    lastError = error.localizedDescription
                    hasSuccessfulWrite = false
                    statusText = "Dot access failed: \(error.localizedDescription)"
                    return false
                }
            }
            if lastError != nil,
               now.timeIntervalSince(lastAttempt) < errorRetrySeconds {
                return false
            }
        }

        lastAttempt = now
        lastWriteSignature = signature
        do {
            try DriveWriter.shared.write(program)
            lastError = nil
            hasSuccessfulWrite = true
            statusText = label
            return true
        } catch {
            lastError = error.localizedDescription
            hasSuccessfulWrite = false
            statusText = "Dot write failed: \(error.localizedDescription)"
            return false
        }
    }
}
