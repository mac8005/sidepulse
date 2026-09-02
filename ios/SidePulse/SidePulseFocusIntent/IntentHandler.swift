import Foundation
import Intents

final class IntentHandler: INExtension, INShareFocusStatusIntentHandling {
    override func handler(for intent: INIntent) -> Any {
        self
    }

    func handle(
        intent: INShareFocusStatusIntent,
        completion: @escaping (INShareFocusStatusIntentResponse) -> Void
    ) {
        let response = INShareFocusStatusIntentResponse(code: .success, userActivity: nil)
        guard let configuration = FocusStatusShared.configuration,
              configuration.isEnabled,
              !configuration.pushToken.isEmpty,
              let focused = intent.focusStatus?.isFocused,
              let url = URL(string: configuration.serverURL)?.appendingPathComponent("dot-focus")
        else {
            completion(response)
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "token": configuration.pushToken,
            "focused": focused,
            "reportedAt": Date().timeIntervalSince1970,
        ])
        request.timeoutInterval = 5

        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.timeoutIntervalForRequest = 5
        sessionConfiguration.timeoutIntervalForResource = 5
        let session = URLSession(configuration: sessionConfiguration)
        session.dataTask(with: request) { _, _, _ in
            session.finishTasksAndInvalidate()
            completion(response)
        }.resume()
    }
}
