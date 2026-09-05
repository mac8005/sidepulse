import Foundation

/// Rate-limit usage of the coding agents on the monitored Mac. The daemon
/// reads it every few minutes (Claude from its OAuth usage endpoint, Codex
/// from the CodexBar CLI) and serves the latest reading on `/usage`; this
/// is that wire format.
struct UsageSnapshot: Codable, Equatable {
    struct Window: Codable, Equatable, Identifiable {
        var id: String
        var label: String
        var usedPercent: Int
        var resetsAt: Double?
        var windowMinutes: Int?
        var pace: String?
    }

    struct Provider: Codable, Equatable, Identifiable {
        var id: String
        var label: String
        var account: String?
        var plan: String?
        var windows: [Window]
        /// Free "Full reset" credits Codex hands out; each wipes the rate
        /// limit once. `resetCreditsExpireAt` is the earliest expiry.
        var resetCredits: Int?
        var resetCreditsExpireAt: Double?
        var updatedAt: Double?
        var error: String?
    }

    var updatedAt: Double?
    var providers: [Provider]
    var error: String?
}

/// Polls the daemon's `/usage` route while the view that owns it is on
/// screen. The daemon refreshes on its own schedule, so polling only picks
/// up its newest cached reading.
@MainActor
final class UsageClient: ObservableObject {
    @Published var snapshot: UsageSnapshot?
    @Published var failure: String?

    private let pollInterval: TimeInterval = 60

    func poll(baseURL: String) async {
        while !Task.isCancelled {
            await fetch(baseURL: baseURL)
            try? await Task.sleep(nanoseconds: UInt64(pollInterval * 1_000_000_000))
        }
    }

    func fetch(baseURL: String) async {
        guard let url = URL(string: baseURL)?.appendingPathComponent("usage") else {
            failure = "Invalid server URL"
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            snapshot = try JSONDecoder().decode(UsageSnapshot.self, from: data)
            failure = nil
        } catch {
            if Task.isCancelled { return }
            // Keep the last reading on screen; only the banner changes.
            failure = error.localizedDescription
        }
    }
}
