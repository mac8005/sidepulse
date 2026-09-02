import Foundation
import Security

struct FocusStatusReportingConfiguration: Codable {
    let serverURL: String
    let pushToken: String
    let isEnabled: Bool
}

/// The Focus intent runs outside the app process. Keep only the three values
/// it needs in a shared Keychain item; the Dot folder bookmark remains
/// private to the main app.
enum FocusStatusShared {
    private static let accessGroupInfoKey = "SidePulseFocusKeychainAccessGroup"
    private static let service = "io.sidepulse.focus-status"
    private static let account = "configuration"

    static func store(serverURL: String, pushToken: String, isEnabled: Bool) {
        guard let accessGroup,
              let data = try? JSONEncoder().encode(
                  FocusStatusReportingConfiguration(
                      serverURL: serverURL,
                      pushToken: pushToken,
                      isEnabled: isEnabled
                  )
              )
        else { return }

        let query = baseQuery(accessGroup: accessGroup)
        let updateStatus = SecItemUpdate(
            query as CFDictionary,
            [kSecValueData: data] as CFDictionary
        )
        guard updateStatus == errSecItemNotFound else { return }

        var item = query
        item[kSecValueData] = data
        item[kSecAttrAccessible] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(item as CFDictionary, nil)
    }

    static var configuration: FocusStatusReportingConfiguration? {
        guard let accessGroup else { return nil }
        var query = baseQuery(accessGroup: accessGroup)
        query[kSecMatchLimit] = kSecMatchLimitOne
        query[kSecReturnData] = true
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data
        else { return nil }
        return try? JSONDecoder().decode(FocusStatusReportingConfiguration.self, from: data)
    }

    private static var accessGroup: String? {
        guard let value = Bundle.main.object(forInfoDictionaryKey: accessGroupInfoKey) as? String,
              !value.isEmpty,
              !value.contains("$(")
        else { return nil }
        return value
    }

    private static func baseQuery(accessGroup: String) -> [CFString: Any] {
        [
            kSecClass: kSecClassGenericPassword,
            kSecAttrAccessGroup: accessGroup,
            kSecAttrService: service,
            kSecAttrAccount: account,
        ]
    }
}
