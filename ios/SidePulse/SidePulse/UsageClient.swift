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
    struct ResetOutcome: Equatable {
        var ok: Bool
        var message: String
    }

    private struct ResetReply: Decodable {
        var ok: Bool
        var message: String
    }

    @Published var snapshot: UsageSnapshot?
    @Published var failure: String?
    @Published var resetOutcome: ResetOutcome?
    @Published var isApplyingReset = false

    private let pollInterval: TimeInterval = 60
    private var baseURL = ""
    /// Idempotency key for the daemon's `/usage/codex-reset`. It is kept
    /// across retries of a request that never got an answer, so a flaky
    /// connection cannot burn two credits for one tap.
    private var pendingResetRequestID: String?

    func poll(baseURL: String) async {
        self.baseURL = baseURL
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

    /// Redeems one free Codex rate-limit reset through the daemon, then
    /// re-reads usage once the daemon has had time to refresh it.
    func applyCodexReset() async {
        guard !isApplyingReset else { return }
        guard let url = URL(string: baseURL)?.appendingPathComponent("usage/codex-reset") else {
            resetOutcome = ResetOutcome(ok: false, message: "Invalid server URL")
            return
        }
        let requestID = pendingResetRequestID ?? UUID().uuidString
        pendingResetRequestID = requestID
        isApplyingReset = true

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["requestId": requestID])
        request.timeoutInterval = 30
        let reply: ResetReply
        do {
            let (data, _) = try await URLSession.shared.data(for: request)
            reply = try JSONDecoder().decode(ResetReply.self, from: data)
        } catch {
            isApplyingReset = false
            resetOutcome = ResetOutcome(ok: false, message: error.localizedDescription)
            return
        }
        // Any decoded reply is definitive; only a transport failure keeps the key.
        pendingResetRequestID = nil
        isApplyingReset = false
        resetOutcome = ResetOutcome(ok: reply.ok, message: reply.message)
        if reply.ok {
            // The daemon re-reads Codex right after a reset; the CLI it uses
            // needs a few seconds.
            try? await Task.sleep(nanoseconds: 15_000_000_000)
            await fetch(baseURL: baseURL)
        }
    }
}
