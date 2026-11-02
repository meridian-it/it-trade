"use client";

import { CheckCircle2, CirclePause, Clock, LoaderCircle, XCircle, type LucideIcon } from "lucide-react";

import { SyncTimers } from "@/components/admin/loading/sync-timers";
import { duration } from "@/lib/admin/labels";
import type { Loading, LoadingStage, StageStatus } from "@/lib/admin/types";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

const ACTIVITY: Record<string, string> = {
  starting: "запускается",
  photos: "грузит фото",
  idle: "очередь пуста",
  paused: "на паузе",
  trial: "пробная партия",
};

const FILL: Record<StageStatus, string> = {
  done: "bg-emerald-500",
  running: "bg-primary",
  waiting: "bg-primary/60",
  paused: "bg-amber-500",
  error: "bg-destructive",
  off: "bg-muted-foreground/25",
};

const ICON: Record<StageStatus, { icon: LucideIcon; tone: string }> = {
  done: { icon: CheckCircle2, tone: "text-emerald-600" },
  running: { icon: LoaderCircle, tone: "text-primary" },
  waiting: { icon: Clock, tone: "text-muted-foreground" },
  paused: { icon: CirclePause, tone: "text-amber-600" },
  error: { icon: XCircle, tone: "text-destructive" },
  off: { icon: Clock, tone: "text-muted-foreground" },
};

type Part = {
  key: string;
  title: string;
  weight: number; // width on the bar: the photo stages are the long ones
  share: number; // 0..1 filled
  status: StageStatus;
  pulse?: boolean; // running without a measure of how far
  note: string;
  sub?: string;
};

/** The whole load as one bar split into its stages, in the order they go: the products, their prices,
 * the photo lists, the main photos, the other photos. Each part fills by its own progress. */
export function OverallPanel({ data }: { data: Loading }) {
  const { overview: o, settings, speed, worker } = data;
  const stages = Object.fromEntries(data.stages.map((s) => [s.key, s])) as Record<string, LoadingStage>;
  const parts = buildParts(stages, o);
  const photosPct = o.photos_total ? (100 * o.photos_done) / o.photos_total : 0;
  const allDone = parts.every((p) => p.status === "done" || p.status === "off");
  const stalled = o.stalled_since != null && !allDone;

  return (
    <section className="@container rounded-2xl bg-background p-5 ring-1 ring-foreground/10 sm:p-6">
      <div className="flex flex-wrap items-end justify-between gap-x-10 gap-y-4">
        <Big
          label="Товаров в каталоге"
          value={formatNumber(o.products)}
          hint={`в наличии ${formatNumber(o.in_stock)}, из них с фото ${formatNumber(o.stock_photos)}`}
        />
        <Big
          label="Фото обработано"
          value={
            <>
              {formatNumber(o.photos_done)}
              <span className="text-lg font-normal text-muted-foreground">
                {" "}
                из {o.photos_estimated && "≈"}
                {formatNumber(o.photos_total)}
              </span>
            </>
          }
          hint={`${pct(photosPct)}% · до ${settings.per_product} фото на товар`}
        />
        <Big
          label="Осталось"
          value={allDone ? "Всё готово" : stalled ? "Загрузка стоит" : o.eta_seconds != null ? `≈ ${duration(o.eta_seconds)}` : "—"}
          tone={stalled ? "text-destructive" : undefined}
          hint={
            stalled
              ? `фото не обрабатываются ${duration(secondsSince(o.stalled_since!))}`
              : settings.paused && !allDone
                ? "загрузка фото на паузе: время — после запуска"
                : speed.photos_per_second
                  ? `при ${speed.photos_per_second.toLocaleString("ru-RU")} фото/с${speed.source === "measured" ? "" : ", ожидаемой"}`
                  : undefined
          }
        />
        <div className="text-sm sm:text-right">
          <p className={cn("font-medium", worker.alive && !stalled ? "text-emerald-700" : "text-destructive")}>
            {!worker.alive ? "Воркер не отвечает" : stalled ? "Фото не грузятся" : "Воркер работает"}
          </p>
          <p className="text-muted-foreground">
            {!worker.alive
              ? "сигнала нет больше 1,5 минут"
              : stalled
                ? "причина — в блоке «Готовность»"
                : (ACTIVITY[worker.activity ?? ""] ?? "")}
          </p>
        </div>
      </div>

      {/* the bar: one part per stage */}
      <div className="mt-6 flex h-10 gap-1" role="img" aria-label={`Фото обработано ${pct(photosPct)}%`}>
        {parts.map((p) => (
          <div key={p.key} className="relative overflow-hidden rounded-lg bg-muted" style={{ flex: `${p.weight} 1 0` }} title={`${p.title}: ${p.note}`}>
            <div
              className={cn("h-full transition-[width] duration-700", FILL[p.status], p.pulse && "animate-pulse")}
              style={{ width: `${Math.round(p.share * 1000) / 10}%` }}
            />
          </div>
        ))}
      </div>

      {/* labels under their parts on a wide panel, a list on a narrow one */}
      <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 @4xl:mt-3 @4xl:flex @4xl:gap-1">
        {parts.map((p, i) => {
          const s = ICON[p.status];
          return (
            <div key={p.key} className="min-w-0 text-sm" style={{ flex: `${p.weight} 1 0` }}>
              <p className="flex items-start gap-1.5 leading-snug font-medium">
                <s.icon className={cn("mt-0.5 size-3.5 shrink-0", s.tone, p.status === "running" && "animate-spin [animation-duration:2.5s]")} />
                <span className="min-w-0 break-words">
                  <span className="text-muted-foreground tabular-nums">{i + 1}. </span>
                  {p.title}
                </span>
              </p>
              <p className="break-words text-muted-foreground tabular-nums">{p.note}</p>
              {p.sub && <p className="break-words text-xs text-muted-foreground">{p.sub}</p>}
            </div>
          );
        })}
      </div>

      <SyncTimers data={data} />
    </section>
  );
}

function buildParts(stages: Record<string, LoadingStage>, o: Loading["overview"]): Part[] {
  const categories = stages.categories;
  const products = stages.products;
  const stock = stages.stock;
  const lists = stages.image_lists;
  const main = stages.main;
  const gallery = stages.gallery;

  // the products: the category tree and the catalog file
  const productsStatus: StageStatus =
    categories?.status === "error" || products?.status === "error"
      ? "error"
      : products?.status === "done"
        ? "done"
        : categories?.status === "running" || products?.status === "running"
          ? "running"
          : "waiting";
  const productsShare = productsStatus === "done" || productsStatus === "error" ? 1 : productsStatus === "running" ? (products?.progress ?? 0.05) : 0;

  return [
    {
      key: "products",
      title: "Товары",
      weight: 1.6,
      share: productsShare,
      status: productsStatus,
      pulse: productsStatus === "running" && products?.progress == null,
      note:
        productsStatus === "error"
          ? "ошибка обмена"
          : productsStatus === "done"
            ? `${formatNumber(o.products)} шт.`
            : productsStatus === "running"
              ? `${productsStep(categories, products)}${products?.progress != null ? ` ${pct(100 * productsShare)}%` : ""}`
              : "ждут",
    },
    {
      key: "stock",
      title: "Цены и наличие",
      weight: 1.6,
      share: stock?.status === "done" || stock?.status === "error" ? 1 : stock?.status === "running" ? 0.5 : 0,
      status: stock?.status ?? "waiting",
      pulse: stock?.status === "running",
      note:
        stock?.status === "done"
          ? `в наличии ${formatNumber(o.in_stock)}`
          : stock?.status === "running"
            ? "идёт"
            : stock?.status === "error"
              ? "ошибка обмена"
              : "после товаров",
    },
    countedPart(lists, "Списки фото", 1.8),
    countedPart(main, "Главные фото", 3),
    countedPart(gallery, "Остальные фото", 4),
  ];
}

/** What the products part is doing now: the category tree, then the catalog file, then the database. */
function productsStep(categories: LoadingStage | undefined, products: LoadingStage | undefined): string {
  if (categories?.status === "running") return "категории";
  if (!products?.phase) return "загрузка";
  // "скачивание файла каталога, 45 МБ" -> "скачивание, 45 МБ"
  return products.phase.startsWith("скачивание") ? products.phase.replace(" файла каталога", "") : "запись в базу";
}

function countedPart(st: LoadingStage | undefined, title: string, weight: number): Part {
  const done = st?.done ?? 0;
  const total = st?.total ?? 0;
  const status = st?.status ?? "waiting";
  const eta = st?.eta_seconds != null && status !== "done" ? `≈ ${duration(st.eta_seconds)}` : undefined;
  return {
    key: st?.key ?? title,
    title,
    weight,
    share: status === "done" ? 1 : total ? Math.min(1, done / total) : 0,
    status,
    note: status === "off" ? "выключено" : `${formatNumber(done)} из ${st?.estimated ? "≈" : ""}${formatNumber(total)}`,
    sub:
      status === "error" && st?.error
        ? st.error
        : status === "paused"
          ? `на паузе${eta ? `, после запуска ${eta}` : ""}`
          : status === "waiting" && eta
            ? `закончится через ${eta}`
            : eta,
  };
}

function Big({ label, value, hint, tone }: { label: string; value: React.ReactNode; hint?: string; tone?: string }) {
  return (
    <div className="min-w-0">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className={cn("mt-0.5 text-3xl font-semibold tracking-tight tabular-nums", tone)}>{value}</p>
      {hint && <p className="mt-0.5 text-sm text-muted-foreground">{hint}</p>}
    </div>
  );
}

function secondsSince(iso: string): number {
  return Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
}

function pct(p: number): string {
  return p > 0 && p < 1 ? "<1" : String(Math.floor(p));
}
