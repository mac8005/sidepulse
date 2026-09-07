import Foundation

enum DriveWriterError: LocalizedError {
    case noFolderSelected
    case bookmarkStale
    case accessDenied
    case textTooLarge(Int)
    case tooManyLines(Int)
    case missingText

    var errorDescription: String? {
        switch self {
        case .noFolderSelected:
            return "Pick the USB drive folder first."
        case .bookmarkStale:
            return "The saved Files permission is stale. Pick the USB folder again."
        case .accessDenied:
            return "iOS did not grant access to the selected USB folder."
        case .textTooLarge(let byteCount):
            return "LEDS.LED is \(byteCount) bytes. Keep it at or below 512 bytes."
        case .tooManyLines(let lineCount):
            return "LEDS.LED has \(lineCount) physical lines. Keep it at or below 20 lines."
        case .missingText:
            return "No LED text was provided."
        }
    }
}

enum DotBrightness {
    static let maximum = 255

    private static let defaultsKey = "dotBrightness"

    static var configuredValue: Int {
        get {
            guard UserDefaults.standard.object(forKey: defaultsKey) != nil else {
                return maximum
            }
            return clamped(UserDefaults.standard.integer(forKey: defaultsKey))
        }
        set {
            UserDefaults.standard.set(clamped(newValue), forKey: defaultsKey)
        }
    }

    static func clamped(_ value: Int) -> Int {
        min(max(value, 0), maximum)
    }

    static func apply(to program: String, brightness: Int? = nil) -> String {
        let value = clamped(brightness ?? configuredValue)
        guard value < maximum else { return program }
        return "brightness \(value)\n\(program)"
    }
}

// Mutable folder state is protected by folderLock; all USB I/O uses ioQueue.
final class DriveWriter: @unchecked Sendable {
    static let shared = DriveWriter()

    private let bookmarkKey = "usbFolderBookmark"
    private let defaultFileName = "LEDS.LED"
    private let maxLEDBytes = 512
    private let maxLEDLines = 20
    private let ioQueue = DispatchQueue(label: "sidepulse.dot.usb", qos: .utility)
    private let folderLock = NSRecursiveLock()
    private var cachedFolderURL: URL?
    private var cachedBookmark: Data?

    private init() {}

    var fileName: String {
        defaultFileName
    }

    var hasSavedFolder: Bool {
        UserDefaults.standard.data(forKey: bookmarkKey) != nil
    }

    var savedBookmark: Data? { UserDefaults.standard.data(forKey: bookmarkKey) }

    var savedFolderDisplayName: String {
        guard let url = try? resolveFolderURL() else {
            return "No USB folder selected"
        }

        return url.path
    }

    func saveFolder(_ url: URL) throws {
        folderLock.lock()
        defer { folderLock.unlock() }
        EventLog.append("Saving USB folder bookmark: \(url.lastPathComponent)")
        let startedAccess = url.startAccessingSecurityScopedResource()
        defer {
            if startedAccess {
                url.stopAccessingSecurityScopedResource()
            }
        }

        let bookmark = try url.bookmarkData(
            options: [],
            includingResourceValuesForKeys: nil,
            relativeTo: nil
        )
        UserDefaults.standard.set(bookmark, forKey: bookmarkKey)
        cachedFolderURL = url
        cachedBookmark = bookmark
        EventLog.append("Saved USB folder bookmark")
    }

    @discardableResult
    func write(
        _ text: String,
        brightness: Int? = nil,
        context: DotWriteContext? = nil,
        bookmark: Data? = nil
    ) async throws -> URL {
        let normalizedProgram = normalizeLEDText(text)
        let trimmed = normalizedProgram.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw DriveWriterError.missingText
        }

        let program = DotBrightness.apply(to: normalizedProgram, brightness: brightness)

        let byteCount = program.data(using: .utf8)?.count ?? 0
        guard byteCount <= maxLEDBytes else {
            throw DriveWriterError.textTooLarge(byteCount)
        }

        let lineCount = physicalLineCount(program)
        guard lineCount <= maxLEDLines else {
            throw DriveWriterError.tooManyLines(lineCount)
        }

        let data = Data(program.utf8)
        let writeContext = context ?? DotNotificationShared.settings.map {
            DotWriteContext(serverURL: $0.configuration.serverURL)
        }
        return try await withCheckedThrowingContinuation { continuation in
            ioQueue.async {
                continuation.resume(with: Result {
                    try self.withFolderAccess(bookmark: bookmark) { folderURL in
                        let targetURL = folderURL.appendingPathComponent(self.fileName, isDirectory: false)
                        try Self.coordinatedWrite(data, to: targetURL, context: writeContext)
                        EventLog.append("Wrote \(data.count) bytes to \(targetURL.lastPathComponent)")
                        return targetURL
                    }
                })
            }
        }
    }

    /// Verify that the saved security-scoped drive is still mounted without
    /// rewriting LEDS.LED or adding a diagnostics-log entry.
    func probeAccess() async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            ioQueue.async {
                continuation.resume(with: Result {
                    try self.withFolderAccess { folderURL in
                        var coordinationError: NSError?
                        var accessError: Error?
                        NSFileCoordinator().coordinate(readingItemAt: folderURL, error: &coordinationError) { url in
                            do {
                                guard try url.checkResourceIsReachable() else {
                                    throw DriveWriterError.accessDenied
                                }
                            } catch { accessError = error }
                        }
                        if let error = coordinationError ?? accessError { throw error }
                    }
                })
            }
        }
    }

    /// The coordinator's URL may differ from the bookmark URL. Write in place:
    /// the Dot firmware consumes LEDS.LED, not an atomically renamed temp file.
    static func coordinatedWrite(_ data: Data, to targetURL: URL, context: DotWriteContext? = nil) throws {
        var coordinationError: NSError?
        var writeError: Error?
        NSFileCoordinator().coordinate(writingItemAt: targetURL, options: [], error: &coordinationError) { url in
            do {
                if let context { try DotNotificationShared.validateWrite(context) }
                try data.write(to: url)
                if let context { try DotNotificationShared.recordWrite(context) }
            }
            catch { writeError = error }
        }
        if let error = coordinationError ?? writeError { throw error }
    }

    private func withFolderAccess<T>(bookmark: Data? = nil, _ operation: (URL) throws -> T) throws -> T {
        folderLock.lock()
        defer { folderLock.unlock() }
        // Reuse the resolved security-scoped URL across background wakes. On
        // failure, discard it and resolve the bookmark once, not indefinitely.
        for attempt in 0...1 {
            var stage = "bookmark"
            do {
                let folderURL = try resolveFolderURL(bookmark: bookmark)
                stage = "permission"
                guard folderURL.startAccessingSecurityScopedResource() else {
                    throw DriveWriterError.accessDenied
                }
                defer { folderURL.stopAccessingSecurityScopedResource() }
                stage = "coordinated access"
                return try operation(folderURL)
            } catch {
                if error is DotNotificationError { throw error }
                cachedFolderURL = nil
                cachedBookmark = nil
                let detail = error as NSError
                EventLog.append("Dot USB \(stage) failed (\(detail.domain):\(detail.code)); \(attempt == 0 ? "retrying once" : "retry deferred")")
                if attempt == 1 { throw error }
            }
        }
        throw DriveWriterError.accessDenied
    }

    private func resolveFolderURL(bookmark explicitBookmark: Data? = nil) throws -> URL {
        folderLock.lock()
        defer { folderLock.unlock() }
        guard let bookmark = explicitBookmark ?? UserDefaults.standard.data(forKey: bookmarkKey) else {
            throw DriveWriterError.noFolderSelected
        }
        if bookmark == cachedBookmark, let cachedFolderURL { return cachedFolderURL }

        var isStale = false
        let url = try URL(
            resolvingBookmarkData: bookmark,
            options: [],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        )

        if isStale {
            guard url.startAccessingSecurityScopedResource() else {
                throw DriveWriterError.bookmarkStale
            }
            defer { url.stopAccessingSecurityScopedResource() }
            let renewed = try url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
            if explicitBookmark == nil { UserDefaults.standard.set(renewed, forKey: bookmarkKey) }
            cachedBookmark = renewed
            EventLog.append("Renewed stale USB folder bookmark")
        } else {
            cachedBookmark = bookmark
        }
        cachedFolderURL = url
        return url
    }

    private func physicalLineCount(_ text: String) -> Int {
        let normalized = text
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        var lines = normalized.split(separator: "\n", omittingEmptySubsequences: false).count
        if normalized.hasSuffix("\n") {
            lines -= 1
        }
        return max(lines, 1)
    }

    private func normalizeLEDText(_ text: String) -> String {
        var output = ""
        var index = text.startIndex

        while index < text.endIndex {
            let character = text[index]
            guard character == "\\",
                  let nextIndex = text.index(index, offsetBy: 1, limitedBy: text.endIndex),
                  nextIndex < text.endIndex else {
                output.append(character)
                index = text.index(after: index)
                continue
            }

            let nextCharacter = text[nextIndex]
            switch nextCharacter {
            case "n":
                output.append("\n")
                index = text.index(after: nextIndex)
            case "r":
                output.append("\r")
                index = text.index(after: nextIndex)
            case "t":
                output.append("\t")
                index = text.index(after: nextIndex)
            case "\\":
                output.append("\\")
                index = text.index(after: nextIndex)
            default:
                output.append(character)
                index = text.index(after: index)
            }
        }

        return output
    }
}
