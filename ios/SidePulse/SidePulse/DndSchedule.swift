import Foundation

struct DndScheduleTransition: Equatable {
    let key: String
    let enabled: Bool
}

struct DndScheduleBoundary: Equatable {
    let date: Date
    let enabled: Bool
}

/// Daily DND window, ported from `status_bar.py`. Times are local "HH:MM".
enum DndSchedule {
    static let defaultStartTime = "22:00"
    static let defaultEndTime = "07:00"

    /// The most recent start/end boundary at or before `now`, looking back
    /// one day so a window that opened yesterday evening is still honoured
    /// this morning. Its key lets the caller apply each boundary exactly once.
    static func latestTransition(startTime: String, endTime: String, now: Date = Date()) -> DndScheduleTransition? {
        guard let start = parse(startTime), let end = parse(endTime) else { return nil }
        var boundaries = [(label: "start", time: start, enabled: true)]
        if start != end {
            boundaries.append((label: "end", time: end, enabled: false))
        }

        let calendar = Calendar.current
        var due: [(date: Date, label: String, enabled: Bool)] = []
        for dayOffset in [-1, 0] {
            guard let day = calendar.date(byAdding: .day, value: dayOffset, to: now) else { continue }
            var components = calendar.dateComponents([.year, .month, .day], from: day)
            for boundary in boundaries {
                components.hour = boundary.time.hour
                components.minute = boundary.time.minute
                components.second = 0
                guard let date = calendar.date(from: components), date <= now else { continue }
                due.append((date, boundary.label, boundary.enabled))
            }
        }
        guard let latest = due.max(by: { $0.date < $1.date }) else { return nil }

        let day = calendar.dateComponents([.year, .month, .day, .hour, .minute], from: latest.date)
        let key = String(
            format: "%04d-%02d-%02d:%@:%02d:%02d",
            day.year ?? 0, day.month ?? 0, day.day ?? 0, latest.label, day.hour ?? 0, day.minute ?? 0
        )
        return DndScheduleTransition(key: key, enabled: latest.enabled)
    }

    static func nextTransition(startTime: String, endTime: String, after now: Date = Date()) -> DndScheduleBoundary? {
        guard let start = parse(startTime), let end = parse(endTime) else { return nil }
        var boundaries = [(time: start, enabled: true)]
        if start != end {
            boundaries.append((time: end, enabled: false))
        }

        let calendar = Calendar.current
        var candidates: [DndScheduleBoundary] = []
        for dayOffset in 0...2 {
            guard let day = calendar.date(byAdding: .day, value: dayOffset, to: now) else { continue }
            var components = calendar.dateComponents([.year, .month, .day], from: day)
            for boundary in boundaries {
                components.hour = boundary.time.hour
                components.minute = boundary.time.minute
                components.second = 0
                if let date = calendar.date(from: components), date > now {
                    candidates.append(DndScheduleBoundary(date: date, enabled: boundary.enabled))
                }
            }
        }
        return candidates.min(by: { $0.date < $1.date })
    }

    static func nextTransitionDate(startTime: String, endTime: String, after now: Date = Date()) -> Date? {
        nextTransition(startTime: startTime, endTime: endTime, after: now)?.date
    }

    static func parse(_ value: String) -> (hour: Int, minute: Int)? {
        let parts = value.split(separator: ":", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
        guard parts.count == 2,
              let hour = Int(parts[0]), let minute = Int(parts[1]),
              (0...23).contains(hour), (0...59).contains(minute)
        else { return nil }
        return (hour, minute)
    }

    static func format(hour: Int, minute: Int) -> String {
        String(format: "%02d:%02d", hour, minute)
    }
}
