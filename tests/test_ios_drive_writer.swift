import Foundation

@main
struct DriveWriterTests {
    static func main() async throws {
        let savedLog = UserDefaults.standard.object(forKey: "eventLog")
        defer { UserDefaults.standard.set(savedLog, forKey: "eventLog") }
        EventLog.clear()
        DispatchQueue.concurrentPerform(iterations: 100) { index in
            EventLog.append("Concurrent USB/ACK log \(index)")
        }
        precondition(EventLog.entries().count == 100, "Concurrent USB and push logging lost events")
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: folder) }
        let target = folder.appendingPathComponent("LEDS.LED")
        try DriveWriter.coordinatedWrite(Data("#4DA3FF".utf8), to: target)
        let first = try String(contentsOf: target, encoding: .utf8)
        precondition(first == "#4DA3FF")
        try DriveWriter.coordinatedWrite(Data("off".utf8), to: target)
        let second = try String(contentsOf: target, encoding: .utf8)
        precondition(second == "off")
        let files = try FileManager.default.contentsOfDirectory(atPath: folder.path)
        precondition(files == ["LEDS.LED"])
        do {
            try DriveWriter.coordinatedWrite(Data("off".utf8), to: folder)
            preconditionFailure("A failed coordinated write must throw")
        } catch {}
        for (text, expected) in [("", "missing"), (String(repeating: "a", count: 513), "bytes"), (String(repeating: "off\n", count: 21), "lines")] {
            do {
                try await DriveWriter.shared.write(text)
                preconditionFailure("Invalid LED text must be rejected")
            } catch let error as DriveWriterError {
                switch (expected, error) {
                case ("missing", .missingText), ("bytes", .textTooLarge), ("lines", .tooManyLines): break
                default: preconditionFailure("Unexpected validation error")
                }
            }
        }
        print("Drive writer tests passed: coordinated create/overwrite, failure propagation, validation")
    }
}
