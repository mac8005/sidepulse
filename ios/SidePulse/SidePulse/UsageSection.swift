import SwiftUI

/// "Usage" section of the Mac Agents screen: one row per provider with a
/// meter per rate-limit window and a live countdown to its reset.
struct UsageSection: View {
    let snapshot: UsageSnapshot?
    let failure: String?

    var body: some View {
        Section {
            if let snapshot, !snapshot.providers.isEmpty {
                ForEach(snapshot.providers) { provider in
                    UsageProviderRow(provider: provider)
                }
            } else {
                Text(snapshot?.error ?? failure ?? "Waiting for data…")
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
        if let message = snapshot?.error ?? failure, snapshot?.providers.isEmpty == false {
            Label(message, systemImage: "exclamationmark.triangle")
                .foregroundStyle(.orange)
        } else if let updatedAt = snapshot?.updatedAt {
            Text("Updated ") + Text(Date(timeIntervalSince1970: updatedAt), style: .relative) + Text(" ago")
        }
    }
}

private struct UsageProviderRow: View {
    let provider: UsageSnapshot.Provider

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
    }

    /// Only Codex reports reset credits; a zero count still tells the user
    /// there is nothing to fall back on when the weekly meter runs out.
    @ViewBuilder
    private func resetCredits(_ credits: Int) -> some View {
        if credits > 0 {
            Label {
                (Text("\(credits) free reset\(credits == 1 ? "" : "s") available")
                    + expiry(provider.resetCreditsExpireAt))
                    .lineLimit(1)
            } icon: {
                Image(systemName: "arrow.counterclockwise.circle.fill")
            }
            .font(.caption)
            .foregroundStyle(.green)
        } else {
            Label("No free resets", systemImage: "arrow.counterclockwise.circle")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func expiry(_ expiresAt: Double?) -> Text {
        guard let expiresAt else { return Text("") }
        return Text(" · first expires in ") + Text(Date(timeIntervalSince1970: expiresAt), style: .relative)
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
