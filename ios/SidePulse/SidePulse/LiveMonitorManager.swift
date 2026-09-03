import Foundation
import Combine
import UserNotifications
#if canImport(ActivityKit)
import ActivityKit
#endif

private struct DotAvailabilityReportSignature: Equatable {
    let serverURL: String
    let token: String
    let availability: DotAvailability
    let dndSchedule: DotDndScheduleMetadata
}

private struct DotDndScheduleMetadata: Equatable {
    let enabled: Bool
    let nextTransitionAt: TimeInterval?
    let nextTransitionEnabled: Bool?
    let identity: String

    @MainActor
    init(model: AppModel, now: Date) {
        enabled = model.dndScheduleEnabled
        identity = "\(model.dndScheduleEnabled)|\(model.dndStartTime)|\(model.dndEndTime)"
        if model.dndScheduleEnabled,
           let transition = DndSchedule.nextTransition(
               startTime: model.dndStartTime,
               endTime: model.dndEndTime,
               after: now
           ) {
            nextTransitionAt = transition.date.timeIntervalSince1970
            nextTransitionEnabled = transition.enabled
        } else {
            nextTransitionAt = nil
            nextTransitionEnabled = nil
        }
    }
}

private struct DotAvailabilityReport {
    let signature: DotAvailabilityReportSignature
    let reportedAt: TimeInterval
}

private struct QueuedDotAvailabilityReport {
    let report: DotAvailabilityReport
    let force: Bool
}

private enum DotAvailabilitySubmitResult {
    case accepted
    case notOwner
    case failed
}

private struct DotDeviceRegistrationRequest {
    let key: String
    let serverURL: String
    let token: Data
    let tokenHex: String
    let model: AppModel
}

private struct LiveActivityResetRequest {
    let activityID: String?
    let activityState: String
    let observedAt: TimeInterval
}

/// Bridges ActivityKit tokens to the `sidepulse live-activity` daemon.
///
/// The daemon owns the activity lifecycle: it starts the Live Activity with a
/// push-to-start token whenever agents wake up, streams content-state updates,
/// and ends it when the host goes idle. All this app does is hand the daemon
/// its tokens.
@MainActor
final class LiveMonitorManager: ObservableObject {
    static let shared = LiveMonitorManager()

    @Published var statusMessage: String = "Off"

    private var observersStarted = false
    private var localActivityStartInProgress = false
    private var nextLocalActivityStartAt = Date.distantPast
    private var scheduledReconcileTask: Task<Void, Never>?
    private var observedActivityIDs: Set<String> = []
    private var knownActivityIDs: [String] = []
    private var selectedActivityID: String?
    private var forceReportingActivityIDs: Set<String> = []
    private var intentionallyEndedActivityIDs: [String] = []
    private var handledTerminalActivityIDs: [String] = []
    private var registeredDotDeviceKey: String?
    private var pendingDotDeviceRegistration: DotDeviceRegistrationRequest?
    private var dotDeviceRegistrationRunning = false
    private var dotRegistrationCancellable: AnyCancellable?
    private var latestDotAvailabilityReport: DotAvailabilityReport?
    private var pendingDotAvailabilityReport: QueuedDotAvailabilityReport?
    private var lastSubmittedDotAvailability: DotAvailabilityReportSignature?
    private var dotAvailabilityReporterRunning = false

    var isSupported: Bool {
        if #available(iOS 17.2, *) { return true }
        return false
    }

    func startIfEnabled(model: AppModel) {
        guard model.liveMonitorEnabled else { return }
        start(model: model)
    }

    func start(model: AppModel) {
        guard #available(iOS 17.2, *) else {
            statusMessage = "Requires iOS 17.2 or later"
            return
        }
        guard !observersStarted else { return }
        observersStarted = true
        statusMessage = "Registering with \(model.liveMonitorServerURL)…"

        // Alert pushes (finished / needs input / blocked) are silent unless
        // the user granted notification permission, which otherwise only the
        // Get Push Token button requests.
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { _, _ in }

        // The daemon sends alert pushes (finished / needs input / blocked)
        // to the app's normal APNs device token.
        if !model.pushToken.isEmpty, let tokenData = Data(hexString: model.pushToken) {
            registerDeviceToken(tokenData, model: model)
        }

        Task {
            for await tokenData in Activity<AgentActivityAttributes>.pushToStartTokenUpdates {
                await self.register(kind: "push_to_start", token: tokenData, model: model)
            }
        }

        for activity in Activity<AgentActivityAttributes>.activities {
            _ = rememberKnownActivityID(activity.id)
        }
        Task {
            for await activity in Activity<AgentActivityAttributes>.activityUpdates {
                let isNewActivity = self.rememberKnownActivityID(activity.id)
                if isNewActivity {
                    EventLog.append(
                        "Live Activity \(activity.id.prefix(8)) newly emitted as \(self.activityStateName(activity.activityState)); preferring replacement"
                    )
                    model.refreshEventLog()
                }
                await self.reconcileActivities(
                    model: model,
                    source: "ActivityKit update",
                    preferredActivityID: isNewActivity ? activity.id : nil
                )
            }
        }
        Task {
            let appIsActive = UIApplication.shared.applicationState == .active
            await self.reconcileActivities(
                model: model,
                source: "monitor start",
                forceReportCurrent: appIsActive
            )
        }
        // A start push whose activity token never reached the daemon
        // leaves an orphan the Mac can no longer end, so the app clears
        // leftovers every time it becomes active — not just at launch.
        NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.reconcileActivities(
                    model: model,
                    source: "app foreground",
                    forceReportCurrent: true
                )
            }
        }
    }

    /// Rechecks ActivityKit in response to a bounded background repair push.
    /// In the background the daemon starts the replacement with push-to-start;
    /// local Activity.request is reserved for a genuinely foreground app.
    func reconcileNow(model: AppModel) async {
        guard model.liveMonitorEnabled else {
            EventLog.append("Live Activity reconcile ignored: monitoring is off")
            model.refreshEventLog()
            return
        }
        guard #available(iOS 17.2, *) else { return }
        if !observersStarted {
            start(model: model)
        }
        let appIsActive = UIApplication.shared.applicationState == .active
        await reconcileActivities(
            model: model,
            source: "server repair request",
            reportAbsenceToServer: !appIsActive,
            forceReportCurrent: true
        )
    }

    /// On a fresh install the APNs token arrives after `start`; the daemon
    /// needs it for the Dot mirror's silent pushes.
    func registerDeviceToken(_ token: Data, model: AppModel) {
        if model.hasFolderAccess {
            ensureDotDeviceRegistration(token: token, model: model)
        }
        guard model.liveMonitorEnabled else { return }
        Task {
            await register(kind: "device", token: token, model: model)
        }
    }

    func observeDotDeviceRegistration(model: AppModel) {
        guard dotRegistrationCancellable == nil else { return }
        dotRegistrationCancellable = model.$hasFolderAccess
            .removeDuplicates()
            .sink { [weak self] hasFolder in
                guard hasFolder else { return }
                MainActor.assumeIsolated {
                    self?.ensureDotDeviceRegistration(model: model)
                }
            }
    }

    /// Selecting the Dot folder can happen long after APNs supplied its
    /// token. Register that saved token at the moment the folder becomes
    /// usable, independently of the Live Activity setting.
    func ensureDotDeviceRegistration(model: AppModel) {
        guard model.hasFolderAccess,
              !model.pushToken.isEmpty,
              let token = Data(hexString: model.pushToken)
        else { return }
        ensureDotDeviceRegistration(token: token, model: model)
    }

    private func ensureDotDeviceRegistration(token: Data, model: AppModel) {
        let tokenHex = token.map { String(format: "%02x", $0) }.joined()
        let serverURL = model.liveMonitorServerURL
        let key = dotDeviceKey(serverURL: serverURL, token: tokenHex)
        guard registeredDotDeviceKey != key else { return }
        if pendingDotDeviceRegistration?.key != key {
            pendingDotDeviceRegistration = DotDeviceRegistrationRequest(
                key: key,
                serverURL: serverURL,
                token: token,
                tokenHex: tokenHex,
                model: model
            )
        }
        guard !dotDeviceRegistrationRunning else { return }
        dotDeviceRegistrationRunning = true
        Task { await drainDotDeviceRegistrations() }
    }

    private func drainDotDeviceRegistrations() async {
        while let registration = pendingDotDeviceRegistration {
            pendingDotDeviceRegistration = nil
            guard isCurrent(registration) else { continue }
            let registered = await register(
                kind: "dot_device",
                token: registration.token,
                model: registration.model,
                serverURL: registration.serverURL,
                isStillCurrent: { self.isCurrent(registration) },
                updatesStatus: false
            )
            // APNs can replace a persisted token while the old registration
            // is in flight. Since registrations are serialized, a queued
            // current token always runs last and remains the elected owner.
            guard registered, isCurrent(registration) else { continue }
            registeredDotDeviceKey = registration.key

            // Availability and registration use separate HTTP requests. A
            // forced current report after registration closes either
            // response-ordering race; it is serialized with other reports.
            if let latestDotAvailabilityReport,
               dotDeviceKey(
                   serverURL: latestDotAvailabilityReport.signature.serverURL,
                   token: latestDotAvailabilityReport.signature.token
               ) == registration.key
            {
                enqueueDotAvailability(
                    DotAvailabilityReport(
                        signature: latestDotAvailabilityReport.signature,
                        reportedAt: Date().timeIntervalSince1970
                    ),
                    force: true
                )
            }
        }
        dotDeviceRegistrationRunning = false
    }

    private func isCurrent(_ registration: DotDeviceRegistrationRequest) -> Bool {
        registration.model.hasFolderAccess
            && registration.model.pushToken == registration.tokenHex
            && registration.model.liveMonitorServerURL == registration.serverURL
    }

    /// Keep exactly one reusable activity: the freshest. Terminal ActivityKit
    /// objects can remain in `Activity.activities`, and an ended activity may
    /// remain visible, but terminal objects are non-updatable and must never
    /// block a replacement or have their token reused.
    @available(iOS 17.2, *)
    private func reconcileActivities(
        model: AppModel,
        source: String,
        reportAbsenceToServer: Bool = false,
        pendingResetRequests: [LiveActivityResetRequest] = [],
        preferredActivityID: String? = nil,
        forceReportCurrent: Bool = false
    ) async {
        let observedAt = Date().timeIntervalSince1970
        let existing = Activity<AgentActivityAttributes>.activities
        let reusable = existing.filter { isReusable($0.activityState) }
        let terminal = existing.filter { !isReusable($0.activityState) }
        var resetRequests = pendingResetRequests
        var forcedCurrentActivity: Activity<AgentActivityAttributes>?

        for activity in terminal {
            let state = activity.activityState
            if consumeTerminalActivity(activity, state: state, model: model, source: source) {
                resetRequests.append(LiveActivityResetRequest(
                    activityID: activity.id,
                    activityState: activityStateName(state),
                    observedAt: observedAt
                ))
            }
        }

        let preferred = preferredActivityID.flatMap { preferredID in
            reusable.first { $0.id == preferredID }
        }
        let selected = preferred ?? selectedActivityID.flatMap { selectedID in
            reusable.first { $0.id == selectedID }
        } ?? reusable.max { lhs, rhs in
            if lhs.content.state.updatedAt != rhs.content.state.updatedAt {
                return lhs.content.state.updatedAt < rhs.content.state.updatedAt
            }
            if lhs.content.state.activeCount != rhs.content.state.activeCount {
                return lhs.content.state.activeCount < rhs.content.state.activeCount
            }
            return lhs.id < rhs.id
        }

        if let selected {
            if preferred != nil, selectedActivityID != selected.id {
                let previous = selectedActivityID.map { String($0.prefix(8)) } ?? "none"
                EventLog.append(
                    "Live Activity replacement \(selected.id.prefix(8)) selected over \(previous)"
                )
                model.refreshEventLog()
            }
            scheduledReconcileTask?.cancel()
            scheduledReconcileTask = nil
            selectedActivityID = selected.id
            for stale in reusable where stale.id != selected.id {
                let activity = stale
                if !intentionallyEndedActivityIDs.contains(activity.id) {
                    intentionallyEndedActivityIDs.append(activity.id)
                    pruneActivityIDHistory(&intentionallyEndedActivityIDs)
                    EventLog.append(
                        "Live Activity \(activity.id.prefix(8)) deduplicating; keeping \(selected.id.prefix(8))"
                    )
                    model.refreshEventLog()
                    Task { await activity.end(nil, dismissalPolicy: .immediate) }
                }
            }
            let shouldForceReport = forceReportCurrent
                && forceReportingActivityIDs.insert(selected.id).inserted
            observe(activity: selected, model: model)
            if shouldForceReport {
                forcedCurrentActivity = selected
            }
        } else {
            selectedActivityID = nil
            if reportAbsenceToServer, resetRequests.isEmpty {
                let state = terminal.first.map { activityStateName($0.activityState) } ?? "none"
                resetRequests.append(LiveActivityResetRequest(
                    activityID: nil,
                    activityState: state,
                    observedAt: observedAt
                ))
            }
        }

        // Selection, observer attachment, and deduplication all happen before
        // yielding to the network. Qualified IDs and observation timestamps
        // let the daemon reject a reset superseded by a replacement token.
        let resetTasks = resetRequests.map { reset in
            Task {
                await self.sendReset(
                    model: model,
                    activityID: reset.activityID,
                    activityState: reset.activityState,
                    activityObservedAt: reset.observedAt
                )
            }
        }
        for task in resetTasks {
            await task.value
        }
        if let forcedCurrentActivity {
            let activityID = forcedCurrentActivity.id
            defer { forceReportingActivityIDs.remove(activityID) }
            if selectedActivityID == activityID,
               isReusable(forcedCurrentActivity.activityState),
               let currentToken = forcedCurrentActivity.pushToken {
                let forceObservedAt = Date().timeIntervalSince1970
                EventLog.append(
                    "Live Activity \(activityID.prefix(8)) force-reporting current token after server repair request"
                )
                model.refreshEventLog()
                _ = await register(
                    kind: "update",
                    token: currentToken,
                    model: model,
                    activityID: activityID,
                    activityObservedAt: forceObservedAt,
                    tokenObservedAt: forceObservedAt,
                    isStillCurrent: {
                        self.selectedActivityID == activityID
                            && self.isReusable(forcedCurrentActivity.activityState)
                            && forcedCurrentActivity.pushToken == currentToken
                    },
                    attemptLimit: 1
                )
            } else {
                EventLog.append(
                    "Live Activity \(activityID.prefix(8)) repair found no current reusable update token; observer remains armed"
                )
                model.refreshEventLog()
            }
        }

        // Activity.request is the foreground recovery path. Foreground
        // reconciles deliberately avoid an absence reset first, because that
        // could wake push-to-start and race this local replacement.
        if selected == nil, UIApplication.shared.applicationState == .active {
            await startActivityLocally(model: model)
        }
    }

    @available(iOS 17.2, *)
    private func consumeTerminalActivity(
        _ activity: Activity<AgentActivityAttributes>,
        state: ActivityState,
        model: AppModel,
        source: String
    ) -> Bool {
        let intentional: Bool
        if let index = intentionallyEndedActivityIDs.firstIndex(of: activity.id) {
            intentionallyEndedActivityIDs.remove(at: index)
            intentional = true
        } else {
            intentional = false
        }
        if selectedActivityID == activity.id {
            selectedActivityID = nil
        }
        observedActivityIDs.remove(activity.id)
        knownActivityIDs.removeAll { $0 == activity.id }

        guard !handledTerminalActivityIDs.contains(activity.id) else {
            return false
        }
        handledTerminalActivityIDs.append(activity.id)
        pruneActivityIDHistory(&handledTerminalActivityIDs)
        let disposition = intentional ? "intentional; ignored" : "requesting replacement"
        EventLog.append(
            "Live Activity \(activity.id.prefix(8)) is \(activityStateName(state)) during \(source); \(disposition)"
        )
        model.refreshEventLog()
        return !intentional
    }

    @available(iOS 17.2, *)
    private func handleTerminalTransition(
        _ activity: Activity<AgentActivityAttributes>,
        state: ActivityState,
        model: AppModel,
        source: String
    ) async {
        var resetRequests: [LiveActivityResetRequest] = []
        if consumeTerminalActivity(activity, state: state, model: model, source: source) {
            resetRequests.append(LiveActivityResetRequest(
                activityID: activity.id,
                activityState: activityStateName(state),
                observedAt: Date().timeIntervalSince1970
            ))
        }
        await reconcileActivities(
            model: model,
            source: source,
            pendingResetRequests: resetRequests
        )
    }

    @available(iOS 17.2, *)
    private func startActivityLocally(model: AppModel) async {
        guard !localActivityStartInProgress else { return }
        let authorization = ActivityAuthorizationInfo()
        guard authorization.areActivitiesEnabled else {
            localActivityStartInProgress = true
            statusMessage = "Live Activities are turned off in Settings"
            EventLog.append("Live Activity start skipped: disabled in Settings")
            model.refreshEventLog()
            await sendReset(
                model: model,
                activityState: "none",
                activityObservedAt: Date().timeIntervalSince1970
            )
            localActivityStartInProgress = false
            return
        }
        let now = Date()
        guard now >= nextLocalActivityStartAt else {
            scheduleReconcile(
                model: model,
                after: nextLocalActivityStartAt.timeIntervalSince(now)
            )
            return
        }
        nextLocalActivityStartAt = now.addingTimeInterval(5)
        localActivityStartInProgress = true
        EventLog.append(
            "Live Activity local start requested (frequent pushes: \(authorization.frequentPushesEnabled))"
        )
        model.refreshEventLog()
        Task {
            let label = await hostLabel(model: model)
            let state = await initialContentState(model: model)

            // A push-to-start may have landed while the snapshot requests
            // were in flight. Let that activity win instead of creating a
            // second one.
            guard reusableActivities().isEmpty else {
                localActivityStartInProgress = false
                await reconcileActivities(model: model, source: "local-start race")
                return
            }
            do {
                let activity = try Activity.request(
                    attributes: AgentActivityAttributes(hostLabel: label),
                    content: ActivityContent(state: state, staleDate: nil),
                    pushType: .token
                )
                localActivityStartInProgress = false
                selectedActivityID = activity.id
                _ = rememberKnownActivityID(activity.id)
                EventLog.append(
                    "Live Activity \(activity.id.prefix(8)) started locally as \(activityStateName(activity.activityState))"
                )
                model.refreshEventLog()
                observe(activity: activity, model: model)
                await reconcileActivities(model: model, source: "local start")
                statusMessage = "Live Activity started"
            } catch {
                localActivityStartInProgress = false
                statusMessage = "Could not start Live Activity: \(error.localizedDescription)"
                EventLog.append("Live Activity local start failed: \(error.localizedDescription)")
                model.refreshEventLog()
                // Local creation is the primary path. Only after it fails do
                // we clear the daemon's stale token and let push-to-start try.
                await sendReset(
                    model: model,
                    activityState: "none",
                    activityObservedAt: Date().timeIntervalSince1970
                )
            }
        }
    }

    @available(iOS 17.2, *)
    private func scheduleReconcile(model: AppModel, after delay: TimeInterval) {
        guard scheduledReconcileTask == nil else { return }
        scheduledReconcileTask = Task { [weak self] in
            let nanoseconds = UInt64(max(0.25, delay) * 1_000_000_000)
            try? await Task.sleep(nanoseconds: nanoseconds)
            guard !Task.isCancelled, let self else { return }
            self.scheduledReconcileTask = nil
            await self.reconcileActivities(model: model, source: "local-start cooldown")
        }
    }

    private func initialContentState(model: AppModel) async -> AgentActivityAttributes.ContentState {
        let fallback = AgentActivityAttributes.ContentState(
            aggregateMode: "idle_ready",
            activeCount: 0,
            agents: [],
            updatedAt: Date().timeIntervalSince1970
        )
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("snapshot"),
              let (data, response) = try? await URLSession.shared.data(from: url),
              let httpResponse = response as? HTTPURLResponse,
              (200..<300).contains(httpResponse.statusCode),
              let snapshot = try? JSONDecoder().decode(AgentSnapshot.self, from: data)
        else { return fallback }

        return AgentActivityAttributes.ContentState(
            aggregateMode: snapshot.aggregateMode,
            activeCount: snapshot.activeCount,
            agents: snapshot.agents.map {
                AgentActivityAttributes.AgentRow(
                    id: $0.id,
                    name: $0.name,
                    mode: $0.mode,
                    detail: $0.detail,
                    provider: $0.provider,
                    cwd: $0.cwd,
                    finishedAt: $0.finishedAt,
                    unread: $0.unread
                )
            },
            updatedAt: snapshot.updatedAt
        )
    }

    /// The daemon's own label for the host; activity attributes are fixed at
    /// creation, so an app-started activity must get this right up front.
    private func hostLabel(model: AppModel) async -> String {
        let fallback = URL(string: model.liveMonitorServerURL)?.host ?? "Mac"
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("health"),
              let (data, _) = try? await URLSession.shared.data(from: url),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let label = object["hostLabel"] as? String,
              !label.isEmpty
        else { return fallback }
        return label
    }

    @available(iOS 17.2, *)
    private func sendReset(
        model: AppModel,
        activityID: String? = nil,
        activityState: String,
        activityObservedAt: TimeInterval
    ) async {
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("register") else {
            EventLog.append("Live Activity reset failed: invalid server URL")
            model.refreshEventLog()
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload: [String: Any] = [
            "kind": "reset",
            "device": await deviceName(),
            "device_id": await deviceID(),
        ]
        if let activityID { payload["activity_id"] = activityID }
        payload["activity_observed_at"] = activityObservedAt
        addActivityContext(activityState: activityState, to: &payload)
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 10
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            if (200..<300).contains(code) {
                EventLog.append("Live Activity reset sent (state: \(activityState))")
            } else {
                EventLog.append("Live Activity reset failed: server error \(code)")
            }
        } catch {
            EventLog.append("Live Activity reset failed: \(error.localizedDescription)")
        }
        model.refreshEventLog()
    }

    /// Confirms the daemon's current Dot command only after the app applied
    /// it. If this request is lost, the daemon's bounded retry delivers the
    /// same command again and the app acknowledges it idempotently.
    func acknowledgeDot(
        commandID: String,
        status: String,
        availability: DotAvailability,
        model: AppModel
    ) async {
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("dot-ack") else {
            EventLog.append("Dot ACK failed: invalid server URL")
            model.refreshEventLog()
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload: [String: Any] = [
            "commandID": commandID,
            "status": status,
        ]
        let now = Date()
        addDotAvailability(
            availability,
            reportedAt: now.timeIntervalSince1970,
            dndSchedule: DotDndScheduleMetadata(model: model, now: now),
            to: &payload
        )
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 8

        for attempt in 1...2 {
            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if (200..<300).contains(code) {
                    let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                    let acknowledged = object?["acknowledged"] as? Bool ?? false
                    let suffix = acknowledged ? "confirmed" : "not current or still pending"
                    EventLog.append("Dot ACK \(commandID.prefix(8)): \(status), \(suffix)")
                    model.refreshEventLog()
                    return
                }
                EventLog.append("Dot ACK server error \(code)")
            } catch {
                EventLog.append("Dot ACK failed: \(error.localizedDescription)")
            }
            guard attempt == 1 else { break }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        model.refreshEventLog()
    }

    /// Reports foreground Dot write results without extending an unchanged
    /// suppression lease every time the 15-second mirror timer fires.
    func reportDotAvailability(_ availability: DotAvailability, model: AppModel) {
        let token = model.pushToken
        guard !token.isEmpty,
              URL(string: model.liveMonitorServerURL) != nil
        else { return }

        if model.hasFolderAccess {
            ensureDotDeviceRegistration(model: model)
        }
        let now = Date()
        let report = DotAvailabilityReport(
            signature: DotAvailabilityReportSignature(
                serverURL: model.liveMonitorServerURL,
                token: token,
                availability: availability,
                dndSchedule: DotDndScheduleMetadata(model: model, now: now)
            ),
            reportedAt: now.timeIntervalSince1970
        )
        latestDotAvailabilityReport = report
        enqueueDotAvailability(report)
    }

    private func enqueueDotAvailability(_ report: DotAvailabilityReport, force: Bool = false) {
        // A forced post-registration submission belongs to the newest state,
        // even if that state changes while another report is in flight.
        let shouldForce = force || pendingDotAvailabilityReport?.force == true
        if !dotAvailabilityReporterRunning,
           !shouldForce,
           report.signature == lastSubmittedDotAvailability {
            return
        }
        pendingDotAvailabilityReport = QueuedDotAvailabilityReport(
            report: report,
            force: shouldForce
        )
        guard !dotAvailabilityReporterRunning else { return }
        dotAvailabilityReporterRunning = true
        Task { await drainDotAvailabilityReports() }
    }

    private func drainDotAvailabilityReports() async {
        while let queued = pendingDotAvailabilityReport {
            pendingDotAvailabilityReport = nil
            let report = queued.report
            if !queued.force, report.signature == lastSubmittedDotAvailability {
                continue
            }
            let result = await submitDotAvailability(report)
            switch result {
            case .accepted, .notOwner:
                lastSubmittedDotAvailability = report.signature
            case .failed:
                break
            }
        }
        dotAvailabilityReporterRunning = false
    }

    private func submitDotAvailability(_ report: DotAvailabilityReport) async -> DotAvailabilitySubmitResult {
        guard let url = URL(string: report.signature.serverURL)?.appendingPathComponent("dot-availability") else {
            return .failed
        }
        var payload: [String: Any] = ["token": report.signature.token]
        addDotAvailability(
            report.signature.availability,
            reportedAt: report.reportedAt,
            dndSchedule: report.signature.dndSchedule,
            to: &payload
        )

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 8

        for attempt in 1...2 {
            do {
                let (_, response) = try await URLSession.shared.data(for: request)
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if (200..<300).contains(code) {
                    return .accepted
                }
                if code == 409 {
                    return .notOwner
                }
            } catch {
                // A second bounded attempt covers a transient Tailscale or
                // network wake without creating a long-running background job.
            }
            guard attempt == 1 else { break }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        return .failed
    }

    private func addDotAvailability(
        _ availability: DotAvailability,
        reportedAt: TimeInterval,
        dndSchedule: DotDndScheduleMetadata,
        to payload: inout [String: Any]
    ) {
        payload["available"] = availability.available
        payload["reportedAt"] = reportedAt
        payload["dndScheduleEnabled"] = dndSchedule.enabled
        if let nextTransitionAt = dndSchedule.nextTransitionAt,
           let nextTransitionEnabled = dndSchedule.nextTransitionEnabled {
            payload["nextDndTransitionAt"] = nextTransitionAt
            payload["nextDndTransitionEnabled"] = nextTransitionEnabled
        }
        if availability.available {
            return
        }
        payload["reason"] = availability.reason
        payload["retryAfterSeconds"] = availability.retryAfterSeconds
    }

    private func dotDeviceKey(serverURL: String, token: String) -> String {
        "\(serverURL)|\(token)"
    }

    @available(iOS 17.2, *)
    private func observe(activity: Activity<AgentActivityAttributes>, model: AppModel) {
        let initialState = activity.activityState
        guard isReusable(initialState) else {
            Task {
                await self.handleTerminalTransition(
                    activity,
                    state: initialState,
                    model: model,
                    source: "observer attachment"
                )
            }
            return
        }
        guard observedActivityIDs.insert(activity.id).inserted else { return }
        let observedAt = Date().timeIntervalSince1970
        EventLog.append(
            "Live Activity \(activity.id.prefix(8)) observing as \(activityStateName(initialState))"
        )
        model.refreshEventLog()
        // Push the current token right away (an activity observed at launch
        // may not re-emit it), then follow rotations.
        if let current = activity.pushToken {
            let tokenObservedAt = Date().timeIntervalSince1970
            Task {
                await self.register(
                    kind: "update",
                    token: current,
                    model: model,
                    activityID: activity.id,
                    activityObservedAt: observedAt,
                    tokenObservedAt: tokenObservedAt
                )
            }
        }
        Task {
            for await tokenData in activity.pushTokenUpdates {
                let tokenObservedAt = Date().timeIntervalSince1970
                await self.register(
                    kind: "update",
                    token: tokenData,
                    model: model,
                    activityID: activity.id,
                    activityObservedAt: observedAt,
                    tokenObservedAt: tokenObservedAt
                )
            }
        }
        Task { [weak self] in
            guard let self else { return }
            var previousState = initialState
            for await state in activity.activityStateUpdates {
                if state != previousState {
                    EventLog.append(
                        "Live Activity \(activity.id.prefix(8)) state \(self.activityStateName(previousState)) -> \(self.activityStateName(state))"
                    )
                    model.refreshEventLog()
                    previousState = state
                }
                if !self.isReusable(state) {
                    await self.handleTerminalTransition(
                        activity,
                        state: state,
                        model: model,
                        source: "state transition"
                    )
                    break
                }
            }
            self.observedActivityIDs.remove(activity.id)
        }
    }

    @discardableResult
    private func register(
        kind: String,
        token: Data,
        model: AppModel,
        activityID: String? = nil,
        activityObservedAt: TimeInterval? = nil,
        tokenObservedAt: TimeInterval? = nil,
        serverURL: String? = nil,
        isStillCurrent: (() -> Bool)? = nil,
        updatesStatus: Bool = true,
        attemptLimit: Int = 5
    ) async -> Bool {
        if kind == "update" {
            guard #available(iOS 17.2, *),
                  isCurrentReusableUpdateActivity(activityID, token: token)
            else { return false }
        }
        if let isStillCurrent, !isStillCurrent() { return false }
        let tokenHex = token.map { String(format: "%02x", $0) }.joined()
        let targetServerURL = serverURL ?? model.liveMonitorServerURL
        guard let url = URL(string: targetServerURL)?.appendingPathComponent("register") else {
            if updatesStatus {
                statusMessage = "Invalid server URL"
            }
            return false
        }
        var payload: [String: Any] = [
            "kind": kind,
            "token": tokenHex,
            "device": await deviceName(),
            "device_id": await deviceID(),
        ]
        if let activityID { payload["activity_id"] = activityID }
        if let activityObservedAt { payload["activity_observed_at"] = activityObservedAt }
        if let tokenObservedAt { payload["token_observed_at"] = tokenObservedAt }
        if #available(iOS 17.2, *) {
            addActivityContext(
                activityState: currentActivityStateName(activityID: activityID),
                to: &payload
            )
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 10

        // A token the daemon never receives leaves an activity it can never
        // update, so a transient failure (cellular blip, Tailscale waking)
        // retries with backoff before giving up.
        var delay: UInt64 = 2
        let attempts = max(1, attemptLimit)
        for attempt in 1...attempts {
            if kind == "update" {
                guard #available(iOS 17.2, *),
                      isCurrentReusableUpdateActivity(activityID, token: token)
                else { return false }
            }
            if let isStillCurrent, !isStillCurrent() { return false }
            do {
                let (_, response) = try await URLSession.shared.data(for: request)
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if (200..<300).contains(code) {
                    if updatesStatus {
                        statusMessage = kind == "push_to_start"
                            ? "Registered — the Mac can now start the Live Activity"
                            : "Live Activity connected"
                    }
                    if kind == "update" || kind == "push_to_start" {
                        EventLog.append(
                            "Live Activity \(kind) token registered (state: \(payload["activity_state"] as? String ?? "unknown"))"
                        )
                        model.refreshEventLog()
                    }
                    return true
                }
                if updatesStatus {
                    statusMessage = "Server error \(code) registering \(kind) token"
                }
            } catch {
                if updatesStatus {
                    statusMessage = "Cannot reach server: \(error.localizedDescription)"
                }
            }
            guard attempt < attempts else { return false }
            try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
            delay *= 2
        }
        return false
    }

    @available(iOS 17.2, *)
    private func reusableActivities() -> [Activity<AgentActivityAttributes>] {
        Activity<AgentActivityAttributes>.activities.filter { isReusable($0.activityState) }
    }

    @available(iOS 17.2, *)
    private func isCurrentReusableUpdateActivity(
        _ activityID: String?,
        token: Data
    ) -> Bool {
        guard let activityID,
              selectedActivityID == activityID,
              let activity = Activity<AgentActivityAttributes>.activities.first(where: { $0.id == activityID })
        else { return false }
        return isReusable(activity.activityState) && activity.pushToken == token
    }

    @available(iOS 17.2, *)
    private func isReusable(_ state: ActivityState) -> Bool {
        if state == .active || state == .stale {
            return true
        }
        if #available(iOS 26.0, *), state == .pending {
            return true
        }
        return false
    }

    @available(iOS 17.2, *)
    private func activityStateName(_ state: ActivityState) -> String {
        if #available(iOS 26.0, *), state == .pending { return "pending" }
        if state == .active { return "active" }
        if state == .stale { return "stale" }
        if state == .ended { return "ended" }
        if state == .dismissed { return "dismissed" }
        return "unknown"
    }

    private func pruneActivityIDHistory(_ history: inout [String]) {
        let limit = 32
        if history.count > limit {
            history.removeFirst(history.count - limit)
        }
    }

    private func rememberKnownActivityID(_ activityID: String) -> Bool {
        guard !knownActivityIDs.contains(activityID) else { return false }
        knownActivityIDs.append(activityID)
        pruneActivityIDHistory(&knownActivityIDs)
        return true
    }

    @available(iOS 17.2, *)
    private func currentActivityStateName(activityID: String? = nil) -> String {
        let activities = Activity<AgentActivityAttributes>.activities
        if let activityID,
           let activity = activities.first(where: { $0.id == activityID }) {
            return activityStateName(activity.activityState)
        }
        if let selectedActivityID,
           let selected = activities.first(where: { $0.id == selectedActivityID }) {
            return activityStateName(selected.activityState)
        }
        if let reusable = activities.first(where: { isReusable($0.activityState) }) {
            return activityStateName(reusable.activityState)
        }
        if let terminal = activities.first {
            return activityStateName(terminal.activityState)
        }
        return "none"
    }

    @available(iOS 17.2, *)
    private func addActivityContext(
        activityState: String,
        to payload: inout [String: Any]
    ) {
        let authorization = ActivityAuthorizationInfo()
        payload["activity_state"] = activityState
        payload["activities_enabled"] = authorization.areActivitiesEnabled
        payload["frequent_pushes_enabled"] = authorization.frequentPushesEnabled
    }

    private func deviceName() async -> String {
        #if canImport(UIKit)
        return await UIDevice.current.name
        #else
        return "iPhone"
        #endif
    }

    private func deviceID() async -> String {
        #if canImport(UIKit)
        let defaultsKey = "liveActivityFallbackDeviceID"
        if let saved = UserDefaults.standard.string(forKey: defaultsKey), !saved.isEmpty {
            return saved
        }
        let identifier = UIDevice.current.identifierForVendor?.uuidString.lowercased()
            ?? UUID().uuidString.lowercased()
        UserDefaults.standard.set(identifier, forKey: defaultsKey)
        return identifier
        #else
        return "sidepulse-ios-unsupported"
        #endif
    }
}

#if canImport(UIKit)
import UIKit
#endif

private extension Data {
    init?(hexString: String) {
        let cleaned = hexString.filter(\.isHexDigit)
        guard cleaned.count % 2 == 0 else { return nil }
        var bytes: [UInt8] = []
        var index = cleaned.startIndex
        while index < cleaned.endIndex {
            let next = cleaned.index(index, offsetBy: 2)
            guard let byte = UInt8(cleaned[index..<next], radix: 16) else { return nil }
            bytes.append(byte)
            index = next
        }
        self.init(bytes)
    }
}
