import Foundation
import Security
#if SIDEPULSE_NOTIFICATION_EXTENSION
import Intents
#endif

struct DotNotificationConfiguration: Codable, Equatable {
    let enabled: Bool
    let serverURL: String
    let pushToken: String
    let bookmark: Data?
    let brightness: Int
    let programs: [String: String]
    let dndEnabled: Bool
    let scheduleEnabled: Bool
    let scheduleStart: String
    let scheduleEnd: String
    let scheduleTransition: String
    let focusEnabled: Bool

    func isDndEnabled(now: Date = Date()) -> Bool {
        guard scheduleEnabled else { return dndEnabled }
        guard let transition = DndSchedule.latestTransition(
            startTime: scheduleStart, endTime: scheduleEnd, now: now
        ) else { return true }
        return transition.key == scheduleTransition ? dndEnabled : transition.enabled
    }
}

struct DotNotificationSettings: Codable {
    let revision: String
    let configuration: DotNotificationConfiguration
}

struct DotWriteReceipt: Codable {
    let serverURL: String
    let sourceUpdatedAt: Double?
    let completedAt: Double
    var writeID: String? = nil
}

struct DotWriteContext {
    let serverURL: String
    var sourceUpdatedAt: Double? = nil
    var configurationRevision: String? = nil
    var extensionStartedAt: Double? = nil
    var deadline: Date? = nil
    var cancellation: DotWriteCancellation? = nil
    var writeID: String = UUID().uuidString
}

final class DotWriteCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    func cancel() { lock.lock(); cancelled = true; lock.unlock() }
    var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
}

enum DotNotificationError: LocalizedError {
    case superseded
    case unavailable
    case expired
    case receiptUnavailable

    var errorDescription: String? {
        switch self {
        case .superseded: return "A newer Dot state or setting is already active."
        case .unavailable: return "The notification cannot access the Dot right now."
        case .expired: return "The notification's Dot update window expired."
        case .receiptUnavailable: return "The Dot write completed but its shared receipt could not be saved."
        }
    }
}

/// The main app and notification extension share only dedicated Keychain items.
/// USB validation and receipt updates must run inside the LEDS.LED coordinator.
enum DotNotificationShared {
    private static let service = "io.sidepulse.dot-notification"
    private static let logLock = NSLock()

    static var settings: DotNotificationSettings? { read("configuration") }

    @discardableResult
    static func store(_ configuration: DotNotificationConfiguration) -> Bool {
        if settings?.configuration == configuration { return true }
        return save(DotNotificationSettings(revision: UUID().uuidString, configuration: configuration), account: "configuration")
    }

    static func latestWrite(serverURL: String) -> DotWriteReceipt? {
        let receipt: DotWriteReceipt? = read("receipt")
        return receipt?.serverURL == serverURL ? receipt : nil
    }

    static func validateWrite(_ context: DotWriteContext, now: Date = Date()) throws {
        let current = settings
        try validate(context, settings: current, receipt: latestWrite(serverURL: context.serverURL), now: now)
        #if SIDEPULSE_NOTIFICATION_EXTENSION
        if current?.configuration.focusEnabled == true,
           INFocusStatusCenter.default.focusStatus.isFocused != false {
            throw DotNotificationError.unavailable
        }
        #endif
    }

    static func validate(
        _ context: DotWriteContext,
        settings: DotNotificationSettings?,
        receipt: DotWriteReceipt?,
        now: Date
    ) throws {
        if context.cancellation?.isCancelled == true { throw DotNotificationError.expired }
        if let deadline = context.deadline, now >= deadline { throw DotNotificationError.expired }
        if let revision = context.configurationRevision {
            guard let settings, settings.configuration.enabled,
                  settings.revision == revision,
                  settings.configuration.serverURL == context.serverURL,
                  !settings.configuration.isDndEnabled(now: now),
                  settings.configuration.brightness > 0
            else { throw DotNotificationError.superseded }
        }
        guard let receipt, receipt.serverURL == context.serverURL else { return }
        if let incoming = context.sourceUpdatedAt, let written = receipt.sourceUpdatedAt,
           incoming < written { throw DotNotificationError.superseded }
        if let started = context.extensionStartedAt, receipt.completedAt > started {
            throw DotNotificationError.superseded
        }
    }

    static func recordWrite(_ context: DotWriteContext) throws {
        let previous = latestWrite(serverURL: context.serverURL)?.sourceUpdatedAt
        let source = [previous, context.sourceUpdatedAt].compactMap { $0 }.max()
        let saved = save(DotWriteReceipt(
            serverURL: context.serverURL, sourceUpdatedAt: source,
            completedAt: Date().timeIntervalSince1970,
            writeID: context.writeID
        ), account: "receipt")
        // Existing foreground writes remain usable without the optional extension.
        if !saved, context.configurationRevision != nil { throw DotNotificationError.receiptUnavailable }
    }

    static func logEntries() -> [String] { read("diagnostics") ?? [] }

    static func appendLog(_ message: String) {
        logLock.lock()
        defer { logLock.unlock() }
        let line = "\(ISO8601DateFormatter().string(from: Date())) Dot notification: \(message)"
        _ = save(Array((logEntries() + [line]).suffix(100)), account: "diagnostics")
    }

    static func clearLog() { _ = save([String](), account: "diagnostics") }

    private static func query(_ account: String) -> [CFString: Any]? {
        guard let group = Bundle.main.object(forInfoDictionaryKey: "SidePulseFocusKeychainAccessGroup") as? String,
              !group.isEmpty, !group.contains("$(") else { return nil }
        return [kSecClass: kSecClassGenericPassword, kSecAttrAccessGroup: group,
                kSecAttrService: service, kSecAttrAccount: account]
    }

    private static func read<T: Decodable>(_ account: String) -> T? {
        guard var query = query(account) else { return nil }
        query[kSecReturnData] = true
        query[kSecMatchLimit] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    private static func save<T: Encodable>(_ value: T, account: String) -> Bool {
        guard var query = query(account), let data = try? JSONEncoder().encode(value) else { return false }
        let status = SecItemUpdate(query as CFDictionary, [kSecValueData: data] as CFDictionary)
        if status == errSecSuccess { return true }
        guard status == errSecItemNotFound else { return false }
        query[kSecValueData] = data
        query[kSecAttrAccessible] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(query as CFDictionary, nil) == errSecSuccess
    }
}

#if SIDEPULSE_NOTIFICATION_EXTENSION
enum EventLog {
    static func append(_ message: String) { DotNotificationShared.appendLog(message) }
}
#endif
