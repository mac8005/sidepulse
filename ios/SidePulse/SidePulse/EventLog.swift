import Foundation
import OSLog

enum EventLog {
    private static let logger = Logger(subsystem: "io.sidepulse.app", category: "SidePulse")
    private static let defaultsKey = "eventLog"
    private static let maxEntries = 500

    static func append(_ message: String) {
        let timestamp = ISO8601DateFormatter().string(from: Date())
        let line = "\(timestamp) \(message)"
        logger.info("\(line, privacy: .public)")

        let defaults = UserDefaults.standard
        var entries = defaults.stringArray(forKey: defaultsKey) ?? []
        entries.append(line)
        if entries.count > maxEntries {
            entries.removeFirst(entries.count - maxEntries)
        }
        defaults.set(entries, forKey: defaultsKey)
    }

    static func entries() -> [String] {
        UserDefaults.standard.stringArray(forKey: defaultsKey) ?? []
    }

    static func clear() {
        UserDefaults.standard.removeObject(forKey: defaultsKey)
    }

    static func export(
        entries: [String],
        details: [String],
        redacting privateValues: [String],
        now: Date = Date()
    ) throws -> URL {
        let timestamp = ISO8601DateFormatter().string(from: now)
        let text = exportText(
            entries: entries,
            details: details,
            redacting: privateValues,
            timestamp: timestamp
        )
        let filename = "SidePulse-Diagnostics-\(timestamp.replacingOccurrences(of: ":", with: "-"))-\(UUID().uuidString.prefix(8)).txt"
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(filename)
        try text.write(to: url, atomically: true, encoding: .utf8)
        return url
    }

    static func exportText(
        entries: [String],
        details: [String],
        redacting privateValues: [String],
        timestamp: String
    ) -> String {
        let header = [
            "SidePulse Diagnostics",
            "Exported: \(timestamp)",
            "Event timestamps are UTC. Settings reflect the time of export.",
            "Known secrets, URLs, file paths and email addresses are redacted. Review before sharing.",
            ""
        ] + details + ["", "Events (oldest first, \(entries.count) entries):"]
        var text = (header + (entries.isEmpty ? ["No events recorded."] : entries))
            .joined(separator: "\n") + "\n"
        for value in privateValues.filter({ !$0.isEmpty }).sorted(by: { $0.count > $1.count }) {
            text = text.replacingOccurrences(of: value, with: "[redacted]")
        }
        let patterns = [
            #"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+"#,
            #"(?i)\b[0-9a-f]{64,}\b"#,
            #"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"#,
            #"/(?:private/)?(?:var|Users)/[^\r\n]+"#
        ]
        for pattern in patterns {
            text = text.replacingOccurrences(
                of: pattern,
                with: "[redacted]",
                options: .regularExpression
            )
        }
        return text
    }
}
