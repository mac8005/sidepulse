import AVFoundation
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
/// iOS only lets the app write to the drive while the process is running.
/// By default `DotKeepalive` holds the process open after the user switches
/// away so the Dot keeps following the agents like the Live Activity does;
/// with that option off the mirror stops on scene-background and turns the
/// Dot off on the way out rather than leaving a stale state glowing.
@MainActor
final class DotStatusMirror: ObservableObject {
    static let shared = DotStatusMirror()

    /// One stream for both the Dot and the Mac Agents screen.
    let stream = AgentStreamClient()

    @Published private(set) var statusText = "Off"

    private var model: AppModel?
    private var cancellables: Set<AnyCancellable> = []
    private var scheduleTimer: Timer?
    private var lastProgram: String?
    private var lastError: String?
    private var lastAttempt: Date = .distantPast
    /// True while a Focus that shares its status with this app is on. iOS
    /// has no in-app change notification, so it is re-read on every sync
    /// (and by the 15 s timer).
    private var focusActive = false
    private var focusAuthorizationRequested = false
    /// True between scene-background and the next scene-active while the
    /// keepalive is holding the process open.
    private var inBackground = false
    /// Matches `AgentLedController.error_retry_seconds` on the Mac.
    private let errorRetrySeconds: TimeInterval = 10
    /// Matches `STATUS_BAR_REFRESH_SECONDS`, which is how often the Mac
    /// checks whether a DND schedule boundary has passed.
    private let scheduleCheckSeconds: TimeInterval = 15

    func start(model: AppModel) {
        inBackground = false
        guard self.model == nil else { return }
        self.model = model
        model.applyDueDndSchedule()
        stream.start(baseURL: model.liveMonitorServerURL)

        // @Published emits on willSet; hop to the next main-queue turn so the
        // sync reads the new values.
        let triggers: [AnyPublisher<Void, Never>] = [
            stream.$snapshot.map { _ in () }.eraseToAnyPublisher(),
            stream.$state.map { _ in () }.eraseToAnyPublisher(),
            model.$kittModeEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$dndEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$focusDndEnabled.map { _ in () }.eraseToAnyPublisher(),
            model.$dotBackgroundEnabled.map { _ in () }.eraseToAnyPublisher(),
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

    /// Scene-background: keep going if the keepalive is holding the process
    /// open, otherwise stop and turn the Dot off.
    func background() {
        guard DotKeepalive.shared.isRunning else {
            suspend()
            return
        }
        inBackground = true
    }

    func suspend() {
        DotKeepalive.shared.stop()
        inBackground = false
        guard model != nil else { return }
        cancellables.removeAll()
        scheduleTimer?.invalidate()
        scheduleTimer = nil
        stream.stop()
        if let lastProgram, lastProgram != DotPrograms.off {
            try? DriveWriter.shared.write(DotPrograms.off)
        }
        lastProgram = nil
        lastError = nil
        focusActive = false
        model = nil
        statusText = "Off"
    }

    /// Focus status needs the user's one-time consent (system prompt) and, per
    /// Focus, "Share Focus Status" enabled in iOS Settings; anything else
    /// reads as "no Focus".
    private func refreshFocusStatus() {
        guard let model, model.focusDndEnabled else {
            focusActive = false
            return
        }
        let center = INFocusStatusCenter.default
        switch center.authorizationStatus {
        case .authorized:
            focusActive = center.focusStatus.isFocused ?? false
        case .notDetermined:
            focusActive = false
            guard !focusAuthorizationRequested else { return }
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
        refreshFocusStatus()
        guard model.hasFolderAccess else {
            DotKeepalive.shared.stop()
            lastProgram = nil
            statusText = "No SidePulse Dot folder selected"
            return
        }
        // Armed while the app is still in front so it is already running when
        // the scene goes to the background.
        if !inBackground {
            if model.dotBackgroundEnabled {
                DotKeepalive.shared.start()
            } else {
                DotKeepalive.shared.stop()
            }
        }

        let state: LedDisplayState
        var label: String
        if model.dndEnabled {
            state = .idle
            label = "DND on — Dot off"
        } else if focusActive {
            state = .idle
            label = "iOS Focus on — Dot off"
        } else if case .failed = stream.state {
            state = .idle
            label = "Mac unreachable — Dot off"
        } else {
            state = stream.snapshot.map { LedDisplayState.forMode($0.aggregateMode) } ?? .idle
            label = state == .working && model.kittModeEnabled ? "Working (KITT)" : state.label
        }
        if let focusAccessHint {
            label += focusAccessHint
        }
        let program = DotPrograms.program(for: state, kittMode: model.kittModeEnabled)

        if program == lastProgram {
            if lastError == nil {
                statusText = label
                return
            }
            if Date().timeIntervalSince(lastAttempt) < errorRetrySeconds {
                return
            }
        }

        lastAttempt = Date()
        lastProgram = program
        do {
            try DriveWriter.shared.write(program)
            lastError = nil
            statusText = label
        } catch {
            lastError = error.localizedDescription
            statusText = "Dot write failed: \(error.localizedDescription)"
            if inBackground {
                // Almost certainly the Dot was unplugged; don't hold the
                // process open for nothing.
                EventLog.append("Dot write failed in background, stopping mirror: \(error.localizedDescription)")
                suspend()
            }
        }
    }
}

/// Loops a silent track so iOS keeps the app running after the user
/// switches away — the one route a plain app has to keep writing to a USB
/// drive from the background. Mixes with other audio and puts nothing in
/// Now Playing.
@MainActor
final class DotKeepalive {
    static let shared = DotKeepalive()

    private var player: AVAudioPlayer?
    private var interruptionObserver: NSObjectProtocol?

    var isRunning: Bool { player != nil }

    func start() {
        guard player == nil else { return }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            try session.setActive(true)
            let player = try AVAudioPlayer(data: Self.silence, fileTypeHint: AVFileType.wav.rawValue)
            player.numberOfLoops = -1
            player.play()
            self.player = player
        } catch {
            EventLog.append("Dot keepalive failed: \(error.localizedDescription)")
            return
        }
        // A phone call or Siri pauses the player; pick it up again afterwards.
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main
        ) { [weak self] note in
            let rawType = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            guard rawType.flatMap(AVAudioSession.InterruptionType.init) == .ended else { return }
            MainActor.assumeIsolated {
                try? AVAudioSession.sharedInstance().setActive(true)
                self?.player?.play()
            }
        }
    }

    func stop() {
        guard let player else { return }
        player.stop()
        self.player = nil
        if let interruptionObserver {
            NotificationCenter.default.removeObserver(interruptionObserver)
            self.interruptionObserver = nil
        }
        try? AVAudioSession.sharedInstance().setActive(false)
    }

    /// One second of 8 kHz 16-bit mono PCM silence as a WAV file.
    private static let silence: Data = {
        let sampleRate: UInt32 = 8000
        let dataSize = sampleRate * 2
        var wav = Data()
        func append<T: FixedWidthInteger>(_ value: T) {
            withUnsafeBytes(of: value.littleEndian) { wav.append(contentsOf: $0) }
        }
        wav.append(contentsOf: "RIFF".utf8)
        append(UInt32(36 + dataSize))
        wav.append(contentsOf: "WAVE".utf8)
        wav.append(contentsOf: "fmt ".utf8)
        append(UInt32(16))          // PCM header length
        append(UInt16(1))           // PCM
        append(UInt16(1))           // mono
        append(sampleRate)
        append(sampleRate * 2)      // byte rate
        append(UInt16(2))           // block align
        append(UInt16(16))          // bits per sample
        wav.append(contentsOf: "data".utf8)
        append(dataSize)
        wav.append(Data(count: Int(dataSize)))
        return wav
    }()
}
