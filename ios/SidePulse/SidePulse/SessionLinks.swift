import Foundation
import Combine
import UIKit

// MARK: - Wire format

/// Where "New session" sends people: for each provider, that provider's own
/// app at its new-session screen, first choice first. The daemon serves this
/// (`GET /session-links`) so a wrong guess is corrected there, without an
/// app build.
struct NewSessionLink: Codable, Equatable, Identifiable {
    var provider: String
    var label: String
    var urls: [String]

    var id: String { provider }
    var candidates: [URL] { urls.compactMap { URL(string: $0) } }
}

// MARK: - Client

/// Fetches the links once per daemon URL while the Mac Agents screen is up.
@MainActor
final class SessionLinksClient: ObservableObject {
    private struct Reply: Decodable {
        var links: [NewSessionLink]
    }

    @Published var links: [NewSessionLink] = []

    func load(baseURL: String) async {
        guard let url = URL(string: baseURL)?.appendingPathComponent("session-links") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let status = (response as? HTTPURLResponse)?.statusCode, (200..<300).contains(status),
              let reply = try? JSONDecoder().decode(Reply.self, from: data)
        else { return }
        links = reply.links
    }
}

/// Opens the first candidate the phone can handle. A universal link counts as
/// handled even without the app (Safari takes it), which is why the custom
/// scheme is the fallback rather than the first choice.
func openFirstAvailable(_ candidates: [URL]) {
    guard let first = candidates.first else { return }
    UIApplication.shared.open(first) { success in
        if !success, candidates.count > 1 {
            openFirstAvailable(Array(candidates.dropFirst()))
        }
    }
}
