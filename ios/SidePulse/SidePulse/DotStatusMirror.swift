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

/// LEDS.LED programs for the 2-LED SidePulse Dot — the same text the Mac app
/// writes (`program_for_display_state` with `led_count=2`), so the Dot looks
/// identical whichever machine it is plugged into.
enum DotPrograms {
    static let off = "off"
    static let askAmber = "#FF3A00"
    static let workingCyan = "#00E5FF"
    static let doneGreen = "#00FF66"

    static func program(for state: LedDisplayState, kittMode: Bool) -> String {
        switch state {
        case .idle:
            return off
        case .ask:
            return "off\n\(askAmber) 1.6s pulse\nrepeat"
        case .done:
            return doneGreen
        case .working:
            return kittMode ? kittScanner(workingCyan) : rolling(workingCyan)
        }
    }

    /// `rolling_program(color, led_count=2)`.
    static func rolling(_ color: String) -> String {
        "off 160ms cosine\n0:\(color) 760ms pulse 0ms; 1:\(color) 760ms pulse 260ms\nrepeat"
    }

    /// `kitt_scanner_program(color, led_count=2)`: scan out, then back.
    static func kittScanner(_ color: String) -> String {
        "off 80ms cosine\n0:\(color) 320ms pulse 0ms; 1:\(color) 320ms pulse 240ms\n0:\(color) 320ms pulse 0ms\nrepeat"
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
    private var lastAttempt: Date = .distantPast
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

    func start(model: AppModel) {
        guard self.model == nil else { return }
        self.model = model
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
            model.$dndEnabled.map { _ in () }.eraseToAnyPublisher(),
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
    /// state; from here on the Mac's pushes update it.
    func suspend() {
        guard model != nil else { return }
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
    /// wake is otherwise invisible. Returns false when the Dot could not be
    /// written.
    @discardableResult
    func applyPush(aggregateMode: String, model: AppModel) -> Bool {
        defer { model.refreshEventLog() }
        // In front the stream is authoritative.
        guard self.model == nil else {
            EventLog.append("Dot push (\(aggregateMode)): app in front, stream in charge")
            return true
        }
        guard model.hasFolderAccess else {
            EventLog.append("Dot push (\(aggregateMode)): no Dot folder selected")
            return false
        }
        model.applyDueDndSchedule()
        // No prompting from the background: an unanswered request reads as
        // "no Focus" until the app is opened again.
        refreshFocusStatus(model: model, allowPrompt: false)
        let resolved = resolve(mode: aggregateMode, unreachable: false, model: model)
        let written = write(
            DotPrograms.program(for: resolved.state, kittMode: model.kittModeEnabled),
            label: resolved.label
        )
        if written {
            EventLog.append("Dot push (\(aggregateMode)): \(resolved.label)")
        } else {
            EventLog.append("Dot push (\(aggregateMode)) failed: \(lastError ?? "unknown error")")
        }
        return written
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
        // Before the folder check so switching the option on asks for Focus
        // access right away.
        refreshFocusStatus(model: model, allowPrompt: true)
        guard model.hasFolderAccess else {
            lastWriteSignature = nil
            statusText = "No SidePulse Dot folder selected"
            return
        }

        var unreachable = false
        if case .failed = stream.state { unreachable = true }
        let resolved = resolve(mode: stream.snapshot?.aggregateMode, unreachable: unreachable, model: model)
        var label = resolved.label
        if let focusAccessHint {
            label += focusAccessHint
        }
        write(DotPrograms.program(for: resolved.state, kittMode: model.kittModeEnabled), label: label)
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
        if signature == lastWriteSignature {
            if lastError == nil {
                statusText = label
                return true
            }
            if Date().timeIntervalSince(lastAttempt) < errorRetrySeconds {
                return false
            }
        }

        lastAttempt = Date()
        lastWriteSignature = signature
        do {
            try DriveWriter.shared.write(program)
            lastError = nil
            statusText = label
            return true
        } catch {
            lastError = error.localizedDescription
            statusText = "Dot write failed: \(error.localizedDescription)"
            return false
        }
    }
}
