import Foundation
import Intents
import UserNotifications

private struct DotCommandResponse: Decodable {
    let dot: DotCommand?
    let available: Bool
    let completionAlertsEnabled: Bool
}

private struct DotCommand: Decodable {
    let commandID: String
    let aggregateMode: String
    let hasUnreadFinished: Bool
    let updatedAt: Double
    let issuedAt: Double
}

/// Keep credentials on the configured server, including on HTTP redirects.
private final class DotSessionDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession, task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) { completionHandler(nil) }
}

final class NotificationService: UNNotificationServiceExtension {
    private let completionLock = NSLock()
    private var handler: ((UNNotificationContent) -> Void)?
    private var content: UNMutableNotificationContent?
    private var work: Task<Void, Never>?
    private let writeCancellation = DotWriteCancellation()

    override func didReceive(
        _ request: UNNotificationRequest,
        withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void
    ) {
        guard let mutable = request.content.mutableCopy() as? UNMutableNotificationContent else {
            contentHandler(request.content)
            return
        }
        handler = contentHandler
        content = mutable
        guard request.content.userInfo["dot"] != nil else {
            finish()
            return
        }
        mutable.sound = nil
        mutable.body = "Open SidePulse to update your Dot."
        let startedAt = Date()
        DotNotificationShared.appendLog("extension started")
        work = Task { await updateDot(startedAt: startedAt) }
    }

    override func serviceExtensionTimeWillExpire() {
        writeCancellation.cancel()
        work?.cancel()
        DotNotificationShared.appendLog("execution window expired; open the app to update the Dot")
        finish()
    }

    private func finish(body: String? = nil) {
        completionLock.lock()
        guard let handler, let content else {
            completionLock.unlock()
            return
        }
        self.handler = nil
        if let body { content.body = body }
        completionLock.unlock()
        handler(content)
    }

    private func updateDot(startedAt: Date) async {
        let deadline = startedAt.addingTimeInterval(20)
        guard let settings = DotNotificationShared.settings,
              settings.configuration.enabled else {
            DotNotificationShared.appendLog("disabled or shared settings unavailable; no USB write")
            finish(body: "Open SidePulse to view the result.")
            return
        }
        let configuration = settings.configuration
        guard let bookmark = configuration.bookmark,
              !configuration.pushToken.isEmpty,
              let baseURL = URL(string: configuration.serverURL),
              ["https", "http"].contains(baseURL.scheme?.lowercased() ?? ""),
              baseURL.host != nil, baseURL.user == nil, baseURL.password == nil else {
            DotNotificationShared.appendLog("incomplete shared connection or USB bookmark")
            finish()
            return
        }
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.timeoutIntervalForRequest = 5
        sessionConfiguration.timeoutIntervalForResource = 6
        sessionConfiguration.requestCachePolicy = .reloadIgnoringLocalCacheData
        let session = URLSession(configuration: sessionConfiguration, delegate: DotSessionDelegate(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        var command: DotCommand?
        do {
            var request = URLRequest(url: baseURL.appendingPathComponent("dot-command"))
            request.setValue(configuration.pushToken, forHTTPHeaderField: "X-SidePulse-Dot-Token")
            let data = try await responseData(session: session, request: request)
            let response = try JSONDecoder().decode(DotCommandResponse.self, from: data)
            guard response.available, response.completionAlertsEnabled, let current = response.dot else {
                DotNotificationShared.appendLog("no current background command; no USB write")
                finish(body: "Open SidePulse to view the result.")
                return
            }
            guard !current.commandID.isEmpty, current.commandID.count <= 128,
                  current.updatedAt.isFinite, current.updatedAt > 0,
                  current.issuedAt.isFinite,
                  let program = configuration.programs[current.aggregateMode + (current.hasUnreadFinished ? ":unread" : ":read")]
            else { throw DotNotificationError.unavailable }
            command = current
            try Task.checkCancellation()
            if configuration.focusEnabled {
                // Unknown Focus status must not turn on hardware against the
                // user's explicit Off during Focus preference.
                guard INFocusStatusCenter.default.focusStatus.isFocused == false else {
                    DotNotificationShared.appendLog("Focus is active or unknown; no USB write")
                    finish(body: "Open SidePulse to view the result.")
                    return
                }
            }
            let context = DotWriteContext(
                serverURL: configuration.serverURL, sourceUpdatedAt: current.updatedAt,
                configurationRevision: settings.revision,
                extensionStartedAt: startedAt.timeIntervalSince1970,
                deadline: deadline, cancellation: writeCancellation
            )
            try await DriveWriter.shared.write(program, brightness: configuration.brightness, context: context, bookmark: bookmark)
            DotNotificationShared.appendLog("command \(current.commandID.prefix(8)) written by extension; unread finished: \(current.hasUnreadFinished)")
            do {
                try await acknowledge(current, status: "written", session: session, baseURL: baseURL)
            } catch {
                DotNotificationShared.appendLog("USB write succeeded but ACK could not be delivered")
            }
            finish(body: "Dot update sent. Open SidePulse to view the result.")
        } catch {
            let detail = error as NSError
            DotNotificationShared.appendLog("update failed (\(detail.domain):\(detail.code)); open the app to update the Dot")
            if let command, !Task.isCancelled, Date() < deadline.addingTimeInterval(-5) {
                try? await acknowledge(command, status: "failed", session: session, baseURL: baseURL)
            }
            finish()
        }
    }

    private func acknowledge(_ command: DotCommand, status: String, session: URLSession, baseURL: URL) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("dot-ack"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Do not overwrite newer main-app Focus, availability, or opt-in reports.
        request.httpBody = try JSONSerialization.data(withJSONObject: ["commandID": command.commandID, "status": status])
        let data = try await responseData(session: session, request: request)
        let result = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        DotNotificationShared.appendLog("ACK \(command.commandID.prefix(8)): \(status), confirmed: \(result?["acknowledged"] as? Bool ?? false)")
    }

    private func responseData(session: URLSession, request: URLRequest) async throws -> Data {
        let (bytes, response) = try await session.bytes(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw DotNotificationError.unavailable }
        var data = Data()
        for try await byte in bytes {
            guard data.count < 65_536 else { throw DotNotificationError.unavailable }
            data.append(byte)
        }
        return data
    }
}
