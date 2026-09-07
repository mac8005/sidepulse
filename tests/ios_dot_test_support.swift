import Combine
import Foundation

// Only OS/app boundaries are replaced. Tests compile the production mirror.
@MainActor
final class AppModel: ObservableObject {
    @Published var dotBrightness = 255
    @Published var dotAppearance = DotAppearance.defaults
    @Published var showFinishedEnabled = false
    @Published var dndEnabled = false
    @Published var dndScheduleEnabled = false
    @Published var dndStartTime = "23:00"
    @Published var dndEndTime = "06:00"
    @Published var focusDndEnabled = false
    @Published var hasFolderAccess = true
    @Published var pushToken = ""
    @Published var liveMonitorServerURL = "https://sidepulse.test/\(UUID().uuidString)"
    func applyDueDndSchedule(now: Date = Date()) {}
    func refreshEventLog() {}
}

struct AgentSnapshot: Codable {
    struct Agent: Codable {
        var mode: String
        var unread: Bool?
    }
    var aggregateMode: String
    var activeCount: Int
    var agents: [Agent]
    var updatedAt: Double
}

@MainActor
final class AgentStreamClient: ObservableObject {
    enum ConnectionState { case idle, connecting, live, failed }
    @Published var snapshot: AgentSnapshot?
    @Published var state = ConnectionState.idle
    func start(baseURL: String, dotToken: String?) {}
    func stop() { snapshot = nil; state = .idle }
}

@MainActor
final class LiveMonitorManager {
    static let shared = LiveMonitorManager()
    func ensureDotDeviceRegistration(model: AppModel) {}
    func reportDotAvailability(_ availability: DotAvailability, model: AppModel) {}
}

enum DotBrightness {
    static var configuredValue = 255
}

@MainActor
final class DriveWriter {
    static let shared = DriveWriter()
    var writes: [String] = []
    var failuresRemaining = 0
    var paused = false
    var continuation: CheckedContinuation<Void, Never>?
    var activeWrites = 0
    var maxConcurrentWrites = 0

    func write(_ program: String) async throws {
        activeWrites += 1
        maxConcurrentWrites = max(maxConcurrentWrites, activeWrites)
        defer { activeWrites -= 1 }
        if paused {
            await withCheckedContinuation { continuation = $0 }
            paused = false
        }
        if failuresRemaining > 0 {
            failuresRemaining -= 1
            throw CocoaError(.fileWriteNoPermission)
        }
        writes.append(program)
    }
    func probeAccess() async throws {}
}

final class SnapshotProtocol: URLProtocol {
    static var snapshot: AgentSnapshot?
    static var requests = 0
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "sidepulse.test" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.requests += 1
        guard let snapshot = Self.snapshot else {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONEncoder().encode(snapshot))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
