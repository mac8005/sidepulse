import Foundation

@main
struct UsageCostTests {
    static func main() throws {
        let legacy = #"{"providers":[{"id":"codex","label":"Codex","windows":[]}]}"#
        let old = try JSONDecoder().decode(UsageSnapshot.self, from: Data(legacy.utf8))
        precondition(old.providers[0].tokenCost == nil)
        precondition(old.providers[0].tokenCostError == nil)
        precondition(old.providers[0].usageUpdatedDate == nil)
        let timestamped = #"{"updatedAt":1800000000,"providers":[{"id":"codex","label":"Codex","windows":[],"updatedAt":1704067200}]}"#
        let dated = try JSONDecoder().decode(UsageSnapshot.self, from: Data(timestamped.utf8))
        precondition(dated.providers[0].usageUpdatedDate == Date(timeIntervalSince1970: 1704067200),
                     "Use the provider reading, not the newer server refresh")
        var invalid = dated.providers[0]
        for value in [0.0, -1.0, Double.infinity, Double.nan] {
            invalid.updatedAt = value
            precondition(invalid.usageUpdatedDate == nil)
        }

        let withCost = #"""
        {"providers":[{"id":"codex","label":"Codex","windows":[],"tokenCost":{
            "today":{"tokens":0,"costUSD":0},
            "last30Days":{"tokens":6927047367,"costUSD":4020.08030176},
            "updatedAt":1704067200,"partial":true,"stale":true
        }}]}
        """#
        let snapshot = try JSONDecoder().decode(UsageSnapshot.self, from: Data(withCost.utf8))
        let cost = snapshot.providers[0].tokenCost!
        precondition(cost.today.tokens == 0 && cost.today.costUSD == 0)
        precondition(cost.last30Days.tokens == 6_927_047_367)
        precondition(cost.last30Days.costUSD == 4020.08030176)
        precondition(cost.partial && cost.stale)
        precondition(cost.todayLabel != "Today", "old estimates must not be labelled Today")
        let unknown = withCost.replacingOccurrences(of: "\"costUSD\":0", with: "\"costUSD\":null")
        let unpriced = try JSONDecoder().decode(UsageSnapshot.self, from: Data(unknown.utf8))
        precondition(unpriced.providers[0].tokenCost!.today.costUSD == nil)
        print("Usage cost tests passed")
    }
}
