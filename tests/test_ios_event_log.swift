import Foundation

@main
struct EventLogExportTests {
    static func main() throws {
        let timestamp = "2026-09-07T08:30:00Z"
        let token = String(repeating: "a", count: 64)
        let secret = "example-secret-with-$pecial.characters"
        let folder = "/private/var/mobile/Library/LiveFiles/My Dot"
        let events = [
            "2026-09-07T07:18:52Z Wrote 83 bytes to LEDS.LED",
            "2026-09-07T07:38:53Z Dot ACK failed: request timed out",
            "Token: \(token); secret: \(secret)",
            "Folder: \(folder)",
            "URL: https://user:password@example.test/ack?token=private-value",
            "Owner: person@example.test",
            "Error: denied /var/mobile/Containers/Data/Private Folder/file.txt"
        ]
        let text = EventLog.exportText(
            entries: events,
            details: ["App: 1.2.3 (4)", "Background App Refresh: denied"],
            redacting: [token, secret, folder, ""],
            timestamp: timestamp
        )
        precondition(text.contains("Exported: \(timestamp)"))
        precondition(text.contains("App: 1.2.3 (4)"))
        precondition(text.contains("Background App Refresh: denied"))
        precondition(text.contains("Events (oldest first, 7 entries):"))
        precondition(text.contains("Wrote 83 bytes to LEDS.LED"))
        precondition(text.contains("Dot ACK failed: request timed out"))
        precondition(text.range(of: events[0])!.lowerBound < text.range(of: events[1])!.lowerBound)
        for value in [token, secret, folder, "https://", "private-value", "person@example.test", "Private Folder"] {
            precondition(!text.contains(value), "Sensitive value was not redacted")
        }

        let unknownToken = String(repeating: "B", count: 64)
        let fallback = EventLog.exportText(
            entries: ["Old token: \(unknownToken)", "File: file:///private/var/mobile/bookmark"],
            details: [], redacting: [], timestamp: timestamp
        )
        precondition(!fallback.contains(unknownToken))
        precondition(!fallback.contains("file:///"))

        let empty = EventLog.exportText(entries: [], details: [], redacting: [], timestamp: timestamp)
        precondition(empty.contains("0 entries"))
        precondition(empty.contains("No events recorded."))

        let now = ISO8601DateFormatter().date(from: timestamp)!
        let url = try EventLog.export(entries: events, details: [], redacting: [secret, folder], now: now)
        defer { try? FileManager.default.removeItem(at: url) }
        precondition(url.pathExtension == "txt")
        precondition(url.lastPathComponent.hasPrefix("SidePulse-Diagnostics-2026-09-07T08-30-00Z-"))
        let exported = try String(contentsOf: url, encoding: .utf8)
        precondition(exported.contains("Wrote 83 bytes to LEDS.LED"))
        precondition(!exported.contains(secret))
        precondition(!exported.contains(token))
        print("EventLog export tests passed: metadata, ordering, redaction, empty log, UTF-8 file")
    }
}
