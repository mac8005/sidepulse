import SwiftUI

/// "Usage" section of the Mac Agents screen: one row per provider with a
/// meter per rate-limit window and a live countdown to its reset.
struct UsageSection: View {
    @ObservedObject var usage: UsageClient

    var body: some View {
        Section {
            if let snapshot = usage.snapshot, !snapshot.providers.isEmpty {
                ForEach(snapshot.providers) { provider in
                    UsageProviderRow(provider: provider, usage: usage)
                }
            } else {
                Text(usage.snapshot?.error ?? usage.failure ?? "Waiting for data…")
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
        if usage.snapshot?.providers.contains(where: { $0.tokenCost != nil }) == true {
            Text("CodexBar API-price estimates from local logs on the monitored Mac, across accounts. Not your subscription bill.")
        }
        if let message = usage.snapshot?.error ?? usage.failure, usage.snapshot?.providers.isEmpty == false {
            Label(message, systemImage: "exclamationmark.triangle")
                .foregroundStyle(.orange)
        }
    }
}

private struct UsageProviderRow: View {
    let provider: UsageSnapshot.Provider
    @ObservedObject var usage: UsageClient
    @State private var confirmingReset = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text(provider.label)
                    .font(.body.weight(.semibold))
                if let plan = provider.plan, !plan.isEmpty {
                    Text(plan.uppercased())
                        .font(.caption2.bold())
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Color(.tertiarySystemFill))
                        .clipShape(Capsule())
                }
                Spacer()
            }

            if let updatedAt = provider.usageUpdatedDate {
                Text("Usage updated \(updatedAt, format: .dateTime.month().day().hour().minute())")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let error = provider.error, provider.windows.isEmpty {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            ForEach(provider.windows) { window in
                UsageWindowRow(window: window)
            }

            if let cost = provider.tokenCost {
                UsageTokenCostRows(cost: cost)
            } else if let error = provider.tokenCostError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if let credits = provider.resetCredits {
                resetCredits(credits)
            }
        }
        .padding(.vertical, 4)
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

    /// Only Codex reports reset credits; a zero count still tells the user
    /// there is nothing to fall back on when the weekly meter runs out.
    @ViewBuilder
    private func resetCredits(_ credits: Int) -> some View {
        HStack(spacing: 8) {
            if credits > 0 {
                Label {
                    VStack(alignment: .leading, spacing: 1) {
                        Text("\(credits) free reset\(credits == 1 ? "" : "s") available")
                        if let expiresAt = provider.resetCreditsExpireAt {
                            Text("First expires \(Date(timeIntervalSince1970: expiresAt), format: .dateTime.day().month())")
                                .foregroundStyle(.secondary)
                        }
                    }
                    .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "arrow.counterclockwise.circle.fill")
                }
                .font(.caption)
                .foregroundStyle(.green)
                Spacer(minLength: 4)
                Button {
                    confirmingReset = true
                } label: {
                    if usage.isApplyingReset {
                        ProgressView()
                            .controlSize(.mini)
                    } else {
                        Text("Apply")
                            .font(.caption.bold())
                    }
                }
                .buttonStyle(.bordered)
                .controlSize(.mini)
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

private struct UsageTokenCostRows: View {
    let cost: UsageSnapshot.TokenCost

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Est. API cost · USD")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            period(cost.todayLabel, value: cost.today)
            period("Last 30 days", value: cost.last30Days)
            if cost.partial {
                Text("Partial estimate: some history or model prices are missing.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if cost.stale {
                Text("Refresh unavailable. Last estimate: \(Date(timeIntervalSince1970: cost.updatedAt), format: .dateTime.month().day().hour().minute())")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.top, 4)
    }

    private func period(_ label: String, value: UsageSnapshot.TokenCost.Period) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(label)
                if let tokens = value.tokens {
                    Text("\(tokens, format: .number.notation(.compactName).precision(.significantDigits(1...3))) tokens")
                        .foregroundStyle(.secondary)
                }
            }
            .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 4)
            if let amount = value.costUSD {
                Text("≈ \(amount, format: .currency(code: "USD"))")
                    .fontWeight(.semibold)
                    .monospacedDigit()
                    .fixedSize()
            } else {
                Text("Not priced")
                    .foregroundStyle(.secondary)
            }
        }
        .font(.caption)
        .accessibilityElement(children: .combine)
    }
}

private struct UsageWindowRow: View {
    let window: UsageSnapshot.Window

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(window.label)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                if let resetsAt = window.resetsAt {
                    (Text("resets in ") + Text(Date(timeIntervalSince1970: resetsAt), style: .relative))
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
                Text("\(window.usedPercent)%")
                    .font(.caption.bold().monospacedDigit())
                    .foregroundStyle(tint)
            }
            ProgressView(value: Double(window.usedPercent), total: 100)
                .tint(tint)
        }
    }

    private var tint: Color {
        switch window.usedPercent {
        case ..<60: return .green
        case ..<85: return .orange
        default: return .red
        }
    }
}
