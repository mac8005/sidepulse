import Foundation

@main
struct DotNotificationTests {
    static func main() throws {
        let now = Date(timeIntervalSince1970: 1_789_000_000)
        let configuration = config()
        let settings = DotNotificationSettings(revision: "current", configuration: configuration)
        let context = DotWriteContext(serverURL: "http://test", sourceUpdatedAt: 100,
            configurationRevision: "current", extensionStartedAt: now.timeIntervalSince1970,
            deadline: now.addingTimeInterval(20))
        try DotNotificationShared.validate(context, settings: settings, receipt: nil, now: now)
        rejects(context, settings: nil, receipt: nil, now: now)
        rejects(context, settings: DotNotificationSettings(revision: "old", configuration: configuration), receipt: nil, now: now)
        rejects(context, settings: DotNotificationSettings(revision: "current", configuration: config(enabled: false)), receipt: nil, now: now)
        rejects(context, settings: DotNotificationSettings(revision: "current", configuration: config(dnd: true)), receipt: nil, now: now)
        rejects(context, settings: DotNotificationSettings(revision: "current", configuration: config(brightness: 0)), receipt: nil, now: now)
        rejects(context, settings: settings, receipt: nil, now: now.addingTimeInterval(20))
        rejects(context, settings: settings,
            receipt: DotWriteReceipt(serverURL: "http://test", sourceUpdatedAt: 101, completedAt: now.timeIntervalSince1970 - 1), now: now)
        rejects(context, settings: settings,
            receipt: DotWriteReceipt(serverURL: "http://test", sourceUpdatedAt: 99, completedAt: now.timeIntervalSince1970 + 1), now: now)
        try DotNotificationShared.validate(context, settings: settings,
            receipt: DotWriteReceipt(serverURL: "http://test", sourceUpdatedAt: 99, completedAt: now.timeIntervalSince1970 - 1), now: now)
        let cancelled = DotWriteCancellation()
        var cancelledContext = context
        cancelledContext.cancellation = cancelled
        cancelled.cancel()
        rejects(cancelledContext, settings: settings, receipt: nil, now: now)
        // Foreground state changes also cannot overwrite a newer extension result.
        rejects(DotWriteContext(serverURL: "http://test", sourceUpdatedAt: 100), settings: settings,
            receipt: DotWriteReceipt(serverURL: "http://test", sourceUpdatedAt: 101, completedAt: now.timeIntervalSince1970), now: now)
        // A manual off has no source revision and must remain possible.
        try DotNotificationShared.validate(DotWriteContext(serverURL: "http://test"), settings: settings,
            receipt: DotWriteReceipt(serverURL: "http://test", sourceUpdatedAt: 101, completedAt: now.timeIntervalSince1970), now: now)

        let calendar = Calendar.current
        let evening = calendar.date(bySettingHour: 23, minute: 30, second: 0, of: now)!
        let morning = calendar.date(bySettingHour: 7, minute: 30, second: 0, of: now)!
        precondition(config(schedule: true).isDndEnabled(now: evening))
        precondition(!config(dnd: true, schedule: true).isDndEnabled(now: morning))
        let eveningKey = DndSchedule.latestTransition(startTime: "23:00", endTime: "06:00", now: evening)!.key
        precondition(!config(schedule: true, transition: eveningKey).isDndEnabled(now: evening), "Manual override must survive until the next boundary")
        print("Dot notification safety tests passed")
    }

    static func config(enabled: Bool = true, brightness: Int = 3, dnd: Bool = false,
                       schedule: Bool = false, transition: String = "") -> DotNotificationConfiguration {
        DotNotificationConfiguration(enabled: enabled, serverURL: "http://test", pushToken: "test",
            bookmark: Data(), brightness: brightness, programs: [:], dndEnabled: dnd,
            scheduleEnabled: schedule, scheduleStart: "23:00", scheduleEnd: "06:00",
            scheduleTransition: transition, focusEnabled: false)
    }

    static func rejects(_ context: DotWriteContext, settings: DotNotificationSettings?, receipt: DotWriteReceipt?, now: Date) {
        do {
            try DotNotificationShared.validate(context, settings: settings, receipt: receipt, now: now)
            preconditionFailure("Unsafe notification write was accepted")
        } catch is DotNotificationError {} catch { preconditionFailure("Unexpected error: \(error)") }
    }
}
