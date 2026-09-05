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
        if let message = usage.snapshot?.error ?? usage.failure, usage.snapshot?.providers.isEmpty == false {
            Label(message, systemImage: "exclamationmark.triangle")
                .foregroundStyle(.orange)
        } else if let updatedAt = usage.snapshot?.updatedAt {
            Text("Updated ") + Text(Date(timeIntervalSince1970: updatedAt), style: .relative) + Text(" ago")
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

            if let error = provider.error, provider.windows.isEmpty {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            ForEach(provider.windows) { window in
                UsageWindowRow(window: window)
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
