import { useState } from "react";
import { Gauge, X } from "lucide-react";
import { api } from "../api/client";
import type { Credential } from "../api/types";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { Button, Field, Input, Notice } from "../ui";

/** 空串 / 非法 → null（= 关闭该护栏）；正数原样返回。 */
function parseLimit(raw: string): number | null {
  const text = raw.trim();
  if (!text) return null;
  const value = Number(text);
  return Number.isFinite(value) && value > 0 ? value : null;
}

/** `{模型: 上限}` 文本解析：每行 `模型=上限`，空行与非法项跳过。 */
function parsePerModel(raw: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const line of raw.split("\n")) {
    const [model, cap] = line.split("=");
    const name = (model ?? "").trim();
    const value = parseLimit(cap ?? "");
    if (name && value !== null) out[name] = value;
  }
  return out;
}

function formatPerModel(mapping: Record<string, number>): string {
  return Object.entries(mapping)
    .map(([model, cap]) => `${model}=${cap}`)
    .join("\n");
}

/**
 * 凭证限额护栏对话框。
 *
 * 四个护栏按北京自然日统计，全部留空 = 不受限（后端删配置行）。`keep_credits`
 * 是账户剩余积分护栏（与当日用量无关，给号留底），其余三个按当日用量判定。
 * 判定在服务端选号前完成，因此保存后需刷新列表才能看到效果。
 */
export function CredentialLimitsDialog({
  credential,
  onClose,
  onSaved,
  onError,
}: {
  credential: Credential;
  onClose: () => void;
  onSaved: (message: string) => void;
  onError: (message: string) => void;
}) {
  const existing = credential.limits;
  const [keepCredits, setKeepCredits] = useState(existing?.keep_credits?.toString() ?? "");
  const [dailyTokens, setDailyTokens] = useState(existing?.daily_tokens?.toString() ?? "");
  const [dailyCredits, setDailyCredits] = useState(existing?.daily_credits?.toString() ?? "");
  const [perModel, setPerModel] = useState(formatPerModel(existing?.per_model_tokens ?? {}));
  const [busy, setBusy] = useState(false);
  const dialogRef = useDialogFocus<HTMLDivElement>(busy ? undefined : onClose);

  const submit = async () => {
    setBusy(true);
    try {
      await api.setCredentialLimits(credential.id, {
        keep_credits: parseLimit(keepCredits),
        daily_tokens: parseLimit(dailyTokens),
        daily_credits: parseLimit(dailyCredits),
        per_model_tokens: parsePerModel(perModel),
      });
      onSaved("限额已保存，下次选号生效。");
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : "限额保存失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
      onClick={() => !busy && onClose()}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="设置限额"
        data-testid="credential-limits-dialog"
        onClick={(event) => event.stopPropagation()}
        className="max-h-[90vh] w-full max-w-lg animate-in overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Gauge className="size-4 text-muted-foreground" />
            限额护栏 · {credential.nickname || credential.id.slice(0, 12)}
          </h2>
          <Button
            size="icon"
            variant="ghost"
            aria-label="关闭"
            data-testid="credential-limits-close"
            disabled={busy}
            onClick={onClose}
          >
            <X className="size-4" />
          </Button>
        </div>

        <Notice tone="muted">
          全部留空 = 不受限。护栏在选号前判定，触顶的凭证不再参与对话流量；
          按模型 token 只停对应模型。当日按北京时间自然日统计。
        </Notice>

        <div className="mt-4 space-y-4">
          <Field
            label="保留积分（账户剩余积分 ≤ 该值时停用）"
            htmlFor="limits-keep-credits-input"
            hint="给号留底：余额跌到该值就换号，避免跑空。留空关闭。"
          >
            <Input
              id="limits-keep-credits-input"
              type="number"
              min="0"
              inputMode="decimal"
              value={keepCredits}
              data-testid="limits-keep-credits"
              placeholder="例如 50"
              onChange={(event) => setKeepCredits(event.target.value)}
            />
          </Field>

          <Field label="每日 token 上限（输入 + 输出）" htmlFor="limits-daily-tokens-input" hint="当日累计达到该值即停用。留空关闭。">
            <Input
              id="limits-daily-tokens-input"
              type="number"
              min="0"
              inputMode="numeric"
              value={dailyTokens}
              data-testid="limits-daily-tokens"
              placeholder="例如 1000000"
              onChange={(event) => setDailyTokens(event.target.value)}
            />
          </Field>

          <Field label="每日积分上限" htmlFor="limits-daily-credits-input" hint="当日累计 credit 到该值即停用。留空关闭。">
            <Input
              id="limits-daily-credits-input"
              type="number"
              min="0"
              inputMode="decimal"
              value={dailyCredits}
              data-testid="limits-daily-credits"
              placeholder="例如 100"
              onChange={(event) => setDailyCredits(event.target.value)}
            />
          </Field>

          <Field
            label="按模型 token 上限"
            htmlFor="limits-per-model-input"
            hint="每行一条 `模型=上限`，如 glm-5=500000。到上限只停该模型。"
          >
            <textarea
              id="limits-per-model-input"
              rows={3}
              value={perModel}
              data-testid="limits-per-model"
              placeholder={"glm-5=500000"}
              className="w-full rounded-lg border border-input bg-background px-2.5 py-2 font-mono text-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
              onChange={(event) => setPerModel(event.target.value)}
            />
          </Field>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            data-testid="limits-save"
            disabled={busy}
            onClick={() => void submit()}
          >
            保存
          </Button>
        </div>
      </div>
    </div>
  );
}
