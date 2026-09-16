import Foundation
import Combine

@MainActor
final class LiveMonitorManager {
    static let shared = LiveMonitorManager()
    var snapshots: [AgentSnapshot] = []
    var urls: [String] = []
    func updateFromStream(_ snapshot: AgentSnapshot, baseURL: String) async {
        snapshots.append(snapshot)
        urls.append(baseURL)
    }
}

@main
struct StreamTest {
    @MainActor static func main() async throws {
        let client = AgentStreamClient()
        let url = CommandLine.arguments[1]
        client.onSnapshot = { snapshot, baseURL in
            await LiveMonitorManager.shared.updateFromStream(snapshot, baseURL: baseURL)
        }
        client.start(baseURL: url)
        for _ in 0..<100 {
            if LiveMonitorManager.shared.snapshots.count >= 2 { break }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        let delivered = LiveMonitorManager.shared.snapshots
        precondition(delivered.count >= 2, "Snapshots must reach ActivityKit bridge")
        precondition(delivered[0].activeCount == 2)
        precondition(delivered[1].activeCount == 1)
        precondition(delivered[1].agents[1].mode == "completed")
        precondition(client.snapshot == delivered[1], "List and activity must get identical data")
        precondition(LiveMonitorManager.shared.urls == [url, url])
        client.stop()
        print("Stream completion regression passed: list and activity both change 2 -> 1")
    }
}
