"use client";

import {
  AlertTriangle,
  CheckCircle2,
  CircleMinus,
  CirclePause,
  Clock,
  Eye,
  ImageOff,
  LoaderCircle,
  RotateCcw,
  ScanSearch,
  Sparkles,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { PhotoGrid } from "@/components/admin/loading/photo-grid";
import { catalogEveryLabel, stockEveryLabel } from "@/components/admin/loading/sync-timers";
import { Card, CardLine } from "@/components/admin/ui";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { apiErrorText } from "@/lib/admin/errors";
import { duration, when } from "@/lib/admin/labels";
import type { CheckStatus, Loading, LoadingCheck, LoadingPhoto, LoadingStage, StageStatus, SyncSettings } from "@/lib/admin/types";
import { api } from "@/lib/api/client";
import { formatNumber, pluralize } from "@/lib/format";
import { cn } from "@/lib/utils";

// --- readiness -----------------------------------------------------------------------------------

const CHECK: Record<CheckStatus, { icon: LucideIcon; tone: string; text: string; label: string }> = {
  error: { icon: XCircle, tone: "bg-destructive/10 text-destructive", text: "text-destructive", label: "ошибка" },
  warn: { icon: AlertTriangle, tone: "bg-amber-500/10 text-amber-600", text: "text-amber-700", label: "стоит проверить" },
  wait: { icon: Clock, tone: "bg-muted text-muted-foreground", text: "text-muted-foreground", label: "пока неизвестно" },
  ok: { icon: CheckCircle2, tone: "bg-emerald-500/10 text-emerald-600", text: "text-emerald-700", label: "в порядке" },
};
const ORDER: CheckStatus[] = ["error", "warn", "wait", "ok"];

/** What has to be right before the photos are loaded: problems first, with what to do; the rest one line each. */
export function Checks({ checks, onRetry }: { checks: LoadingCheck[]; onRetry: () => Promise<void> }) {
  const errors = checks.filter((c) => c.status === "error").length;
  const warnings = checks.filter((c) => c.status === "warn").length;
  const sorted = [...checks].sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status));
  const problems = sorted.filter((c) => c.status !== "ok");
  const fine = sorted.filter((c) => c.status === "ok");
  return (
    <Card
      title="Готовность"
      description="Что проверить перед загрузкой фото"
      action={
        <span
          className={cn(
            "rounded-full px-2.5 py-1 text-xs font-medium",
            errors ? CHECK.error.tone : warnings ? CHECK.warn.tone : CHECK.ok.tone,
          )}
        >
          {errors
            ? `${errors} ${pluralize(errors, ["ошибка", "ошибки", "ошибок"])}`
            : warnings
              ? `${warnings} ${pluralize(warnings, ["замечание", "замечания", "замечаний"])}`
              : "всё в порядке"}
        </span>
      }
    >
      {problems.map((c) => {
        const s = CHECK[c.status];
        return (
          <CardLine key={c.key} icon={s.icon} tone={s.tone} title={c.title} aside={<span className={s.text}>{s.label}</span>}>
            <p className="text-sm text-muted-foreground">{c.detail}</p>
            {c.hint && <p className="mt-1 text-xs text-muted-foreground/80">{c.hint}</p>}
            {c.action === "retry" && <RetryButton onRetry={onRetry} className="mt-2" />}
          </CardLine>
        );
      })}
      {fine.length > 0 && (
        <ul className="space-y-2 py-4 text-sm">
          {fine.map((c) => (
            <li key={c.key} className="flex gap-2.5">
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-600" aria-label="в порядке" />
              <span className="min-w-0">
                <span className="font-medium">{c.title}</span>
                <span className="text-muted-foreground"> — {c.detail}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/** Runs the failed supplier syncs again at once instead of after the pause. */
function RetryButton({ onRetry, className }: { onRetry: () => Promise<void>; className?: string }) {
  const [busy, setBusy] = useState(false);
  return (
    <Button
      variant="outline"
      size="sm"
      className={className}
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        await onRetry();
        setBusy(false);
      }}
    >
      {busy ? <LoaderCircle className="animate-spin" /> : <RotateCcw />}
      Повторить сейчас
    </Button>
  );
}

// --- stages --------------------------------------------------------------------------------------

const STAGE: Record<StageStatus, { icon: LucideIcon; tone: string; text: string; label: string }> = {
  done: { icon: CheckCircle2, tone: "bg-emerald-500/10 text-emerald-600", text: "text-emerald-700", label: "Готово" },
  running: { icon: LoaderCircle, tone: "bg-primary/10 text-primary", text: "text-primary", label: "Идёт" },
  waiting: { icon: Clock, tone: "bg-muted text-muted-foreground", text: "text-muted-foreground", label: "В очереди" },
  paused: { icon: CirclePause, tone: "bg-amber-500/10 text-amber-600", text: "text-amber-700", label: "На паузе" },
  error: { icon: XCircle, tone: "bg-destructive/10 text-destructive", text: "text-destructive", label: "Ошибка" },
  off: { icon: CircleMinus, tone: "bg-muted text-muted-foreground", text: "text-muted-foreground", label: "Выключено" },
};

export function Stages({ stages }: { stages: LoadingStage[] }) {
  return (
    <Card title="Этапы" description="По порядку: сначала товары и цены, потом фото">
      {stages.map((st, i) => (
        <StageRow key={st.key} stage={st} n={i + 1} />
      ))}
    </Card>
  );
}

function StageRow({ stage: st, n }: { stage: LoadingStage; n: number }) {
  const s = STAGE[st.status];
  const counted = st.total != null && st.done != null && st.total > 0;
  const pct = counted ? Math.min(100, (100 * st.done!) / st.total!) : 0;
  return (
    <CardLine
      icon={s.icon}
      tone={s.tone}
      spin={st.status === "running"}
      title={
        <>
          <span className="mr-1.5 text-muted-foreground tabular-nums">{n}.</span>
          {st.title}
        </>
      }
      aside={<span className={s.text}>{stageNote(st, s.label)}</span>}
    >
      <p className="text-sm text-muted-foreground">{st.detail}</p>
      {counted && (
        <Bar
          pct={pct}
          done={st.status === "done"}
          label={
            <>
              {formatNumber(st.done!)} из {st.estimated && "≈"}
              {formatNumber(st.total!)}
              <span className="ml-1.5 text-muted-foreground">{pct < 1 && pct > 0 ? "<1" : Math.floor(pct)}%</span>
            </>
          }
        />
      )}
      {st.status === "running" && st.progress != null && (
        <Bar
          pct={st.progress * 100}
          label={
            <>
              {st.phase && <span className="mr-1.5 text-muted-foreground">{st.phase}</span>}
              {Math.floor(st.progress * 100)}%
            </>
          }
        />
      )}
      {st.status === "running" && st.progress == null && st.phase && <p className="mt-1 text-sm text-muted-foreground">{st.phase}</p>}
      {st.failed > 0 && <p className="mt-1 text-xs text-destructive">не скачались: {formatNumber(st.failed)}</p>}
      {st.error && (
        <p className="mt-1 text-sm break-words text-destructive">
          {st.error}
          {st.retry_at !== undefined && ( // a supplier sync: it runs again by itself
            <span className="text-muted-foreground">
              {" · "}
              {st.retry_requested ? "повтор в течение 30 секунд" : st.retry_at ? `повтор ${retryWhen(st.retry_at)}` : "повтор скоро"}
            </span>
          )}
        </p>
      )}
    </CardLine>
  );
}

function Bar({ pct, done, label }: { pct: number; done?: boolean; label: React.ReactNode }) {
  return (
    <div className="mt-2 flex items-center gap-3">
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
        <div className={cn("h-full rounded-full transition-[width] duration-700", done ? "bg-emerald-500" : "bg-primary")} style={{ width: `${pct}%` }} />
      </div>
      <span className="shrink-0 text-sm tabular-nums">{label}</span>
    </div>
  );
}

function retryWhen(at: string): string {
  const s = (new Date(at).getTime() - Date.now()) / 1000;
  return s <= 30 ? "в течение 30 секунд" : `через ${duration(s)}`;
}

function stageNote(st: LoadingStage, label: string): string {
  const eta = st.eta_seconds != null ? `≈ ${duration(st.eta_seconds)}` : null;
  if (st.status === "done") return st.last_success_at ? `${label}, ${when(st.last_success_at)}` : label;
  if (st.status === "running") return eta ? `${label}, осталось ${eta}` : label;
  if (st.status === "waiting") return eta ? `${label}, закончится через ${eta}` : label;
  if (st.status === "paused") return eta ? `${label}; после запуска ${eta}` : label;
  return label;
}

// --- photos with problems ------------------------------------------------------------------------

type PhotoKind = "left" | "no_mark" | "trial";

/** The photos that did not download and the ones whose watermark removal is worth a look, in one card. */
export function ProblemPhotos({ data, onChange }: { data: Loading; onChange: () => void }) {
  const { failed, quality } = data;
  const [shown, setShown] = useState<"left" | "no_mark" | null>(null);
  const [busy, setBusy] = useState(false);

  async function retry() {
    setBusy(true);
    try {
      const { queued } = await api<{ queued: number }>("/api/admin/images/retry-failed", { method: "POST" });
      toast.success(`Снова в очереди: ${formatNumber(queued)} фото`);
      onChange();
    } catch (e) {
      toast.error(apiErrorText(e));
    } finally {
      setBusy(false);
    }
  }

  const count = (n: number, bad: boolean) => <span className={cn("text-lg font-semibold tabular-nums", bad && n > 0 && "text-destructive")}>{formatNumber(n)}</span>;
  return (
    <Card title="Фото с проблемами" description={`После снятия знака у каждого фото измеряется, сколько знака осталось; проверено ${formatNumber(quality.checked)}.`}>
      <CardLine icon={ImageOff} tone="bg-destructive/10 text-destructive" title="Не скачались" aside={count(failed.total, true)}>
        {failed.reasons.length > 0 && (
          <ul className="text-sm">
            {failed.reasons.map((r) => (
              <li key={r.reason} className="flex justify-between gap-3">
                <span className="min-w-0 break-words">{r.reason}</span>
                <span className="shrink-0 tabular-nums">{formatNumber(r.count)}</span>
              </li>
            ))}
          </ul>
        )}
        <p className="text-sm text-muted-foreground">После трёх попыток. Если не скачалось главное фото, главным становится следующее.</p>
        <Button variant="outline" size="sm" className="mt-2" onClick={retry} disabled={busy || !failed.total}>
          {busy ? <LoaderCircle className="animate-spin" /> : <RotateCcw />}
          Повторить
        </Button>
      </CardLine>
      <CardLine icon={Sparkles} tone="bg-amber-500/10 text-amber-600" title="Остался заметный след знака" aside={count(quality.visible_left, true)}>
        <p className="text-sm text-muted-foreground">Остаток больше {quality.left_threshold}: буквы могут быть видны. Худшие — первыми.</p>
        <ShowButton disabled={!quality.visible_left} onClick={() => setShown("left")} />
      </CardLine>
      <CardLine icon={ScanSearch} title="Знак не найден" aside={count(quality.no_mark, false)}>
        <p className="text-sm text-muted-foreground">
          Обычно на фото знака и правда нет, но почти чёрный знак на чёрном товаре детектор иногда пропускает.
        </p>
        <ShowButton disabled={!quality.no_mark} onClick={() => setShown("no_mark")} />
      </CardLine>
      <PhotosDialog
        kind={shown}
        threshold={quality.left_threshold}
        title={shown === "left" ? "Остался заметный след знака" : "Знак не найден"}
        onOpenChange={(o) => !o && setShown(null)}
      />
    </Card>
  );
}

function ShowButton({ disabled, onClick }: { disabled: boolean; onClick: () => void }) {
  return (
    <Button variant="outline" size="sm" className="mt-2" disabled={disabled} onClick={onClick}>
      <Eye />
      Показать
    </Button>
  );
}

/** Photos of a kind in a wide dialog, loaded when it opens; each opens in the lab. */
export function PhotosDialog({
  kind,
  title,
  threshold,
  onOpenChange,
}: {
  kind: PhotoKind | null;
  title: string;
  threshold: number;
  onOpenChange: (open: boolean) => void;
}) {
  const [photos, setPhotos] = useState<{ kind: PhotoKind; list: LoadingPhoto[] } | null>(null);
  useEffect(() => {
    if (!kind) return;
    let cancelled = false;
    api<LoadingPhoto[]>(`/api/admin/loading/photos?kind=${kind}&limit=${kind === "trial" ? 500 : 120}`).then(
      (list) => !cancelled && setPhotos({ kind, list }),
      (e) => !cancelled && toast.error(apiErrorText(e)),
    );
    return () => {
      cancelled = true;
    };
  }, [kind]);
  const list = photos && photos.kind === kind ? photos.list : null;
  return (
    <Dialog open={kind !== null} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] gap-5 overflow-y-auto p-5 sm:max-w-6xl sm:p-6">
        <DialogHeader>
          <DialogTitle className="text-lg font-semibold">{title}</DialogTitle>
          <DialogDescription>Клик по фото открывает его в лаборатории — с оригиналом и сравнением до/после.</DialogDescription>
        </DialogHeader>
        {list ? (
          <PhotoGrid photos={list} threshold={threshold} />
        ) : (
          <div className="grid h-40 place-items-center">
            <LoaderCircle className="size-5 animate-spin text-muted-foreground" />
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// --- what happens by itself ----------------------------------------------------------------------

export function AutomationsDialog({
  perProduct,
  version,
  sync,
  open,
  onOpenChange,
}: {
  perProduct: number;
  version: number;
  sync: SyncSettings;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const items = [
    "Сначала главные фото всех товаров в наличии, потом вторые, третьи и так далее — товары появляются в каталоге с фото как можно раньше.",
    `У товара грузятся и показываются только первые ${perProduct} фото: главное первым, дальше в порядке поставщика.`,
    "Фото, которое открыли на сайте раньше, чем до него дошла очередь, обрабатывается сразу — на долю загрузки, которую не берёт воркер.",
    `Когда выходит новая версия обработки (сейчас ${version}), фото старых версий обрабатываются заново после новых фото. До замены на сайте видна старая версия.`,
    "Если поставщик недоступен, фото остаются в очереди, а воркер ждёт минуту. Если у поставщика нет файла, фото помечается ошибкой.",
    `Списки фото товаров в наличии перечитываются раз в 14 дней, цены и наличие — ${stockEveryLabel(sync.stock_every_min)}, каталог — ${catalogEveryLabel(sync)} (меняется в настройках).`,
  ];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] gap-5 overflow-y-auto p-5 sm:max-w-xl sm:p-6">
        <DialogHeader>
          <DialogTitle className="text-lg font-semibold">Что происходит само</DialogTitle>
          <DialogDescription>
            Подробно — в статье{" "}
            <Link href="/admin/docs/catalog-loading" className="text-primary">
              «Загрузка каталога»
            </Link>
            .
          </DialogDescription>
        </DialogHeader>
        <ul className="list-disc space-y-2 pl-5 text-sm">
          {items.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      </DialogContent>
    </Dialog>
  );
}
