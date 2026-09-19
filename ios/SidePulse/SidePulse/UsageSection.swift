import SwiftUI

/// What is left of each agent's rate limits. One card per provider, one meter
/// per window: the percentage and the countdown to its reset sit on the same
/// line as the label, so a glance answers "can I still work today".
struct UsageSection: View {
    @ObservedObject var usage: UsageClient
    var isDense = false

    var body: some View {
        Section {
            if let snapshot = usage.snapshot, !snapshot.providers.isEmpty {
                ForEach(snapshot.providers) { provider in
                    UsageProviderRow(provider: provider, usage: usage, isDense: isDense)
                }
            } else if let message = usage.snapshot?.error ?? usage.failure {
                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            } else {
                Text("Reading usage…")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        } header: {
            Text("Usage")
        } footer: {
            footer
        }
    }

    @ViewBuilder
    private var footer: some View {
        if let message = usage.snapshot?.error ?? usage.failure,
           usage.snapshot?.providers.isEmpty == false {
            Label(message, systemImage: "exclamationmark.triangle")
                .foregroundStyle(.orange)
        } else if usage.snapshot?.providers.contains(where: { $0.tokenCost != nil }) == true, !isDense {
            Text("API-price estimates read from local logs on the monitored Mac, across accounts. Not your subscription bill.")
        }
    }
}

private struct UsageProviderRow: View {
    let provider: UsageSnapshot.Provider
    @ObservedObject var usage: UsageClient
    var isDense = false
    @State private var confirmingReset = false

    var body: some View {
        VStack(alignment: .leading, spacing: isDense ? 8 : 10) {
            header

            ForEach(provider.windows) { window in
                UsageMeter(window: window, isDense: isDense)
            }

            if let error = provider.error, provider.windows.isEmpty {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            if !isDense, let cost = provider.tokenCost {
                UsageCost(cost: cost)
            }

            if let credits = provider.resetCredits {
                resetCredits(credits)
            }
        }
        .padding(.vertical, isDense ? 2 : 4)
        .confirmationDialog(
            "Use one free Codex reset now?",
            isPresented: $confirmingReset,
            titleVisibility: .visible
        ) {
            Button("Apply reset") {
                Task { await usage.applyCodexReset() }
            }
        } message: {
            Text("This clears your current Codex usage limits and consumes one of your \(provider.resetCredits ?? 0) free resets.")
        }
    }

    private var header: some View {
        HStack(spacing: 6) {
            Text(provider.label)
                .font(.subheadline.weight(.semibold))
            if let plan = provider.plan, !plan.isEmpty {
                Text(plan.capitalized)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            if let updatedAt = provider.updatedAt {
                Text(compactAge(since: updatedAt))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .monospacedDigit()
                    .accessibilityLabel("Read \(compactAge(since: updatedAt)) ago")
            }
        }
    }

    /// Only Codex reports reset credits; a zero count still tells the user
    /// there is nothing to fall back on when the weekly meter runs out.
    @ViewBuilder
    private func resetCredits(_ credits: Int) -> some View {
        HStack(spacing: 8) {
            if credits > 0 {
                Label {
                    Text("\(credits) free reset\(credits == 1 ? "" : "s")")
                } icon: {
                    Image(systemName: "arrow.counterclockwise.circle.fill")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                Spacer(minLength: 4)
                Button {
                    confirmingReset = true
                } label: {
                    if usage.isApplyingReset {
                        ProgressView().controlSize(.mini)
                    } else {
                        Text("Apply").font(.caption.weight(.semibold))
                    }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(usage.isApplyingReset)
            } else {
                Label("No free resets", systemImage: "arrow.counterclockwise.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        if let outcome = usage.resetOutcome {
            Label(outcome.message, systemImage: outcome.ok ? "checkmark.circle" : "exclamationmark.circle")
                .font(.caption)
                .foregroundStyle(outcome.ok ? .green : .orange)
        }
    }
}

/// One rate-limit window: label, countdown, percentage, bar. The colour is a
/// threshold, and the percentage next to it says the same thing in numbers.
private struct UsageMeter: View {
    let window: UsageSnapshot.Window
    var isDense = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(window.label)
                    .font(.subheadline)
                    .lineLimit(1)
                Spacer(minLength: 4)
                if let resetsAt = window.resetsAt, !isDense {
                    Text(Date(timeIntervalSince1970: resetsAt), style: .relative)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
                Text("\(window.usedPercent)%")
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(tint)
                    .frame(width: 44, alignment: .trailing)
            }
            Capsule()
                .fill(.quaternary)
                .frame(height: 3)
                .overlay(alignment: .leading) {
                    GeometryReader { proxy in
                        Capsule()
                            .fill(tint)
                            .frame(width: proxy.size.width * fraction)
                    }
                }
                .clipShape(Capsule())
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(window.label)
        .accessibilityValue(accessibilityValue)
    }

    private var fraction: Double {
        min(1, max(0, Double(window.usedPercent) / 100))
    }

    /// Neutral until it matters: orange once a window is nearly spent, red
    /// once it all but is.
    private var tint: Color {
        switch window.usedPercent {
        case ..<80: return .secondary
        case ..<95: return .orange
        default: return .red
        }
    }

    private var accessibilityValue: String {
        var value = "\(window.usedPercent) percent used"
        if let resetsAt = window.resetsAt {
            let minutes = Int((resetsAt - Date().timeIntervalSince1970) / 60)
            if minutes > 0 {
                value += minutes < 120
                    ? ", resets in \(minutes) minutes"
                    : ", resets in \(minutes / 60) hours"
            }
        }
        return value
    }
}

private struct UsageCost: View {
    let cost: UsageSnapshot.TokenCost

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            period(cost.todayLabel, cost.today)
            period("Last 30 days", cost.last30Days)
            if cost.partial || cost.stale {
                Text(cost.stale
                     ? "Last estimate \(Date(timeIntervalSince1970: cost.updatedAt), format: .dateTime.hour().minute())"
                     : "Partial estimate: some history or model prices are missing.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.top, 2)
    }

    private func period(_ label: String, _ value: UsageSnapshot.TokenCost.Period) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .foregroundStyle(.secondary)
            Spacer(minLength: 4)
            if let amount = value.costUSD {
                Text(amount, format: .currency(code: "USD"))
                    .monospacedDigit()
            } else {
                Text("Not priced").foregroundStyle(.tertiary)
            }
        }
        .font(.caption)
        .accessibilityElement(children: .combine)
    }
}
