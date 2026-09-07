import Foundation

@main
struct DotRecoveryTests {
    @MainActor
    static func main() async throws {
        if CommandLine.arguments.contains("--programs") {
            var programs: [[String: String]] = []
            for animation in DotAnimation.allCases {
                for unread in [false, true] {
                    let appearance = DotAppearance(animation: animation)
                    programs.append([
                        "animation": animation.rawValue,
                        "unread": String(unread),
                        "program": DotPrograms.program(for: .working, appearance: appearance, finiteWorking: true, showFinished: true, hasUnreadFinished: unread)
                    ])
                }
            }
            print(String(data: try JSONEncoder().encode(programs), encoding: .utf8)!)
            return
        }
        URLProtocol.registerClass(SnapshotProtocol.self)
        defer { URLProtocol.unregisterClass(SnapshotProtocol.self) }
        let keys = ["eventLog", "lastSuccessfulDotStreamUpdatedAt", "lastSuccessfulDotStreamServerURL"]
        let saved = keys.map { UserDefaults.standard.object(forKey: $0) }
        defer {
            for (key, value) in zip(keys, saved) { UserDefaults.standard.set(value, forKey: key) }
        }
        let writer = DriveWriter.shared
        let host = UUID().uuidString
        defer {
            UserDefaults.standard.removeObject(forKey: "lastDotPushCommandID.\(host)")
            UserDefaults.standard.removeObject(forKey: "lastDotPushIssuedAt.\(host)")
        }
        let model = AppModel()
        let mirror = DotStatusMirror()
        func push(_ mode: String, issued: Double, unread: Bool = false) async -> DotPushApplyOutcome {
            await mirror.applyPush(aggregateMode: mode, hasUnreadFinished: unread, commandID: "command-\(issued)", issuedAt: issued, sourceUpdatedAt: issued, host: host, model: model)
        }

        // Failed USB writes are not ACKed or suppressed for an hour.
        writer.failuresRemaining = 1
        let failed = await push("working", issued: 100)
        precondition(failed.result == .failed && !failed.availability.available)
        precondition(failed.availability.retryAfterSeconds == 300)
        precondition(writer.writes.isEmpty)
        let recovered = await push("working", issued: 100)
        precondition(recovered.result == .written && recovered.availability.available)
        precondition(writer.contexts.last??.serverURL == model.liveMonitorServerURL)
        precondition(writer.contexts.last??.sourceUpdatedAt == 100)

        // Main writes A at t1, the extension writes B at t2, and main resumes
        // at t3. B must invalidate A even though its timestamp is before t3.
        let mainWriteID = writer.contexts.last!!.writeID
        let resumedAt = Date(timeIntervalSince1970: 3)
        let extensionReceipt = DotWriteReceipt(
            serverURL: model.liveMonitorServerURL, sourceUpdatedAt: 101,
            completedAt: 2, writeID: "extension-write"
        )
        precondition(DotStatusMirror.sharedWriteInvalidatesCache(
            extensionReceipt, lastWriteID: mainWriteID, lastProgramWrite: resumedAt
        ), "An extension write before async resumption must invalidate the old program")
        let ownReceipt = DotWriteReceipt(
            serverURL: model.liveMonitorServerURL, sourceUpdatedAt: 100,
            completedAt: 1, writeID: mainWriteID
        )
        precondition(!DotStatusMirror.sharedWriteInvalidatesCache(
            ownReceipt, lastWriteID: mainWriteID, lastProgramWrite: resumedAt
        ))
        let legacyReceipt = DotWriteReceipt(
            serverURL: model.liveMonitorServerURL, sourceUpdatedAt: 100, completedAt: 4
        )
        precondition(DotStatusMirror.sharedWriteInvalidatesCache(
            legacyReceipt, lastWriteID: mainWriteID, lastProgramWrite: resumedAt
        ), "Old receipts still use timestamp invalidation")
        precondition(!DotStatusMirror.sharedWriteInvalidatesCache(
            legacyReceipt, lastWriteID: mainWriteID, lastProgramWrite: Date(timeIntervalSince1970: 5)
        ))

        // Retried working pushes renew the finite program, even if the ACK
        // of the first delivery was lost. Merely probing the folder is not enough.
        let beforeRetry = writer.writes.count
        let duplicate = await push("working", issued: 100)
        precondition(duplicate.result == .written)
        precondition(writer.writes.count == beforeRetry + 1)

        // A persisted/newer timestamp cannot prove a watchdog is still running.
        SnapshotProtocol.snapshot = AgentSnapshot(aggregateMode: "working", activeCount: 1, agents: [], updatedAt: 100)
        let beforeStale = writer.writes.count
        let refreshed = await push("completed", issued: 90)
        precondition(refreshed.result == .written && refreshed.availability.available)
        precondition(SnapshotProtocol.requests == 1 && writer.writes.count == beforeStale + 1)
        precondition(writer.writes.last!.contains("repeat "))
        precondition(writer.contexts.last??.sourceUpdatedAt == 100, "Stale push must use the fetched source revision")
        SnapshotProtocol.snapshot = nil
        let unavailable = await push("completed", issued: 80)
        precondition(unavailable.result == .failed)
        precondition(writer.writes.count == beforeStale + 1)

        // A successful refresh must use the latest state, including all-read.
        SnapshotProtocol.snapshot = AgentSnapshot(aggregateMode: "completed", activeCount: 0, agents: [], updatedAt: 200)
        let allRead = await push("working", issued: 70)
        precondition(allRead.result == .written && writer.writes.last == "off")
        precondition(writer.contexts.last??.sourceUpdatedAt == 200, "An all-read server state still has a source revision")

        // Intentional suppression keeps its lease, even if writing off fails.
        model.dndEnabled = true
        writer.failuresRemaining = 1
        let suppressed = await push("working", issued: 210)
        // An unchanged off program may already be current; use a fresh mirror
        // to exercise the actual off-write failure.
        precondition(suppressed.availability.reason == "dnd")
        let fresh = await DotStatusMirror().applyPush(aggregateMode: "working", model: model)
        precondition(fresh.result == .failed && fresh.availability.reason == "dnd")
        precondition(fresh.availability.retryAfterSeconds == 86400)
        model.dndEnabled = false
        model.hasFolderAccess = false
        let noFolder = await push("working", issued: 220)
        precondition(noFolder.result == .noFolder && noFolder.availability.reason == "no_folder")
        model.hasFolderAccess = true

        // An in-flight working write must complete before a newer off command.
        writer.paused = true
        let first = Task { await push("working", issued: 300) }
        for _ in 0..<1000 {
            if writer.continuation != nil { break }
            await Task.yield()
        }
        precondition(writer.continuation != nil)
        let second = Task { await push("completed", issued: 301) }
        for _ in 0..<10 { await Task.yield() }
        precondition(writer.activeWrites == 1 && writer.maxConcurrentWrites == 1)
        writer.continuation?.resume()
        writer.continuation = nil
        let firstResult = await first.value
        let secondResult = await second.value
        precondition(firstResult.result == .written && secondResult.result == .written)
        precondition(writer.writes.last == "off" && writer.maxConcurrentWrites == 1)

        // Foreground working writes must already be finite: suspension cannot
        // depend on squeezing another USB write into the background transition.
        let foreground = DotStatusMirror()
        foreground.stream.snapshot = AgentSnapshot(aggregateMode: "working", activeCount: 1, agents: [], updatedAt: 400)
        foreground.stream.state = .live
        foreground.start(model: model)
        for _ in 0..<1000 {
            if writer.writes.last != "off" { break }
            await Task.yield()
        }
        precondition(writer.writes.last!.contains("repeat "))
        let beforeSuspend = writer.writes.count
        foreground.suspend()
        for _ in 0..<20 { await Task.yield() }
        precondition(writer.writes.count == beforeSuspend)
        foreground.stream.snapshot = AgentSnapshot(aggregateMode: "working", activeCount: 1, agents: [], updatedAt: 401)
        foreground.stream.state = .live
        foreground.start(model: model)
        for _ in 0..<1000 {
            if writer.writes.count > beforeSuspend { break }
            await Task.yield()
        }
        precondition(writer.writes.count > beforeSuspend, "Reopening must rewrite after a USB reconnect")
        foreground.suspend()

        // Foreground completion receipts confirm the green/blue USB program,
        // not just reception of the snapshot. No new silent push is needed.
        model.showFinishedEnabled = true
        let receipts = LiveMonitorManager.shared
        let mixed = DotStatusMirror()
        mixed.stream.snapshot = AgentSnapshot(aggregateMode: "working", activeCount: 1, agents: [.init(mode: "completed", unread: true)], updatedAt: 450, dotCommandID: "mixed")
        mixed.stream.state = .live
        mixed.start(model: model)
        for _ in 0..<1000 {
            if receipts.acknowledgedCommands.contains("mixed") { break }
            await Task.yield()
        }
        precondition(receipts.acknowledgedCommands.contains("mixed"))
        precondition(receipts.programsAtAcknowledgement.last!.contains("0:#39D98A"))
        precondition(receipts.programsAtAcknowledgement.last!.contains("1:#4DA3FF"))
        precondition(writer.contexts.last??.sourceUpdatedAt == 450)
        mixed.stream.snapshot = AgentSnapshot(aggregateMode: "completed", activeCount: 0, agents: [], updatedAt: 451, dotCommandID: "all-read")
        for _ in 0..<1000 {
            if receipts.acknowledgedCommands.contains("all-read") { break }
            await Task.yield()
        }
        precondition(receipts.acknowledgedCommands.contains("all-read"))
        precondition(receipts.programsAtAcknowledgement.last == "off")
        precondition(writer.contexts.last??.sourceUpdatedAt == 451)
        mixed.suspend()

        // A Focus/DND change during USB I/O must not be overwritten by a
        // later ready acknowledgement for the foreground snapshot.
        let suppressedReceipt = DotStatusMirror()
        suppressedReceipt.stream.snapshot = AgentSnapshot(aggregateMode: "working", activeCount: 1, agents: [], updatedAt: 460, dotCommandID: "suppressed-during-write")
        suppressedReceipt.stream.state = .live
        writer.paused = true
        suppressedReceipt.start(model: model)
        for _ in 0..<1000 {
            if writer.continuation != nil { break }
            await Task.yield()
        }
        precondition(writer.continuation != nil)
        model.dndEnabled = true
        writer.continuation?.resume()
        writer.continuation = nil
        for _ in 0..<1000 {
            if writer.writes.last == "off" { break }
            await Task.yield()
        }
        for _ in 0..<20 { await Task.yield() }
        precondition(!receipts.acknowledgedCommands.contains("suppressed-during-write"))
        precondition(writer.contexts.last??.serverURL == model.liveMonitorServerURL)
        precondition(writer.contexts.last??.sourceUpdatedAt == nil, "Suppression must not reuse the previous session revision")
        suppressedReceipt.suspend()
        model.dndEnabled = false

        // A due refresh with no successful write must still respect the local
        // error back-off. Frequent SSE events must not hammer an absent drive.
        writer.failuresRemaining = 2
        let failingForeground = DotStatusMirror()
        failingForeground.stream.snapshot = AgentSnapshot(aggregateMode: "working", activeCount: 1, agents: [], updatedAt: 500, dotCommandID: "failed-write")
        failingForeground.stream.state = .live
        failingForeground.start(model: model)
        for _ in 0..<1000 {
            if writer.failuresRemaining < 2 { break }
            await Task.yield()
        }
        for revision in 501...510 {
            failingForeground.stream.snapshot?.updatedAt = Double(revision)
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        precondition(writer.failuresRemaining == 1, "Repeated snapshots bypassed USB error back-off")
        precondition(!receipts.acknowledgedCommands.contains("failed-write"), "A failed USB write must not be acknowledged")
        failingForeground.suspend()

        // A newer extension write can win after the main-app source check but
        // before coordinated USB access. That is a skipped command, not bad USB.
        writer.failuresRemaining = 0
        writer.forcedError = .superseded
        let racingModel = AppModel()
        let racingPush = DotStatusMirror()
        let beforeSupersededPush = writer.writes.count
        let supersededPush = await racingPush.applyPush(
            aggregateMode: "completed", hasUnreadFinished: true,
            commandID: "superseded-push", issuedAt: 700, sourceUpdatedAt: 700,
            host: host, model: racingModel
        )
        precondition(supersededPush.result == .failed && supersededPush.acknowledgementStatus == "failed")
        precondition(supersededPush.availability == .ready, "A newer extension write must not disable healthy USB delivery")
        precondition(writer.writes.count == beforeSupersededPush)
        precondition(UserDefaults.standard.string(forKey: "lastDotPushCommandID.\(host)") != "superseded-push")
        precondition(racingPush.statusText == "Waiting for current Mac state")
        writer.forcedError = nil
        let afterSupersededPush = await racingPush.applyPush(
            aggregateMode: "completed", hasUnreadFinished: true,
            commandID: "current-push", issuedAt: 701, sourceUpdatedAt: 701,
            host: host, model: racingModel
        )
        precondition(afterSupersededPush.result == .written && afterSupersededPush.availability == .ready)
        precondition(writer.writes.count == beforeSupersededPush + 1, "Superseded work must not leave a successful cache entry or error back-off")

        writer.forcedError = .superseded
        let beforeSupersededStream = writer.writes.count
        let beforeAvailability = receipts.reportedAvailability.count
        let beforeSourceRevision = UserDefaults.standard.double(forKey: "lastSuccessfulDotStreamUpdatedAt")
        let racingStream = DotStatusMirror()
        racingStream.stream.snapshot = AgentSnapshot(aggregateMode: "completed", activeCount: 0, agents: [], updatedAt: 750, dotCommandID: "superseded-stream")
        racingStream.stream.state = .live
        racingStream.start(model: racingModel)
        for _ in 0..<1000 {
            if receipts.reportedAvailability.count > beforeAvailability { break }
            await Task.yield()
        }
        precondition(receipts.reportedAvailability.count > beforeAvailability)
        precondition(receipts.reportedAvailability.dropFirst(beforeAvailability).allSatisfy { $0 == .ready })
        precondition(!receipts.acknowledgedCommands.contains("superseded-stream"))
        precondition(writer.writes.count == beforeSupersededStream)
        precondition(UserDefaults.standard.double(forKey: "lastSuccessfulDotStreamUpdatedAt") == beforeSourceRevision)
        writer.forcedError = nil
        racingStream.stream.snapshot = AgentSnapshot(aggregateMode: "completed", activeCount: 0, agents: [], updatedAt: 751, dotCommandID: "current-stream")
        for _ in 0..<1000 {
            if receipts.acknowledgedCommands.contains("current-stream") { break }
            await Task.yield()
        }
        precondition(receipts.acknowledgedCommands.contains("current-stream"), "Current state must be writable immediately after a superseded skip")
        precondition(writer.contexts.last??.sourceUpdatedAt == 751)
        racingStream.suspend()
        print("Dot recovery tests passed: USB failure, retry, stale refresh, offline ACK, all-read, DND, serialization, finite foreground, retry throttle, superseded write race, write identity cache")
    }
}
