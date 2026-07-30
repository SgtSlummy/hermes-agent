import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useState,
} from "react";
import {
  BookOpen,
  Layers3,
  Play,
  RefreshCw,
  Route,
  ShieldCheck,
  Sparkles,
  Square,
  Users,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type {
  OccultDashboardStatus,
  OccultReadingStatus,
} from "@/lib/api";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Toast } from "@/components/Toast";
import { useToast } from "@/hooks/useToast";
import { usePageHeader } from "@/contexts/usePageHeader";

const EMPTY_STATUS: OccultDashboardStatus = {
  enabled: false,
  configured: false,
  connected: false,
  agents: [],
  routes: [],
  decks: [],
  pairings: [],
};

function statusTone(connected: boolean): string {
  return connected
    ? "border-success/30 bg-success/10 text-success"
    : "border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-300";
}

function labelForState(value: unknown): string {
  return typeof value === "string" && value.trim() ? value : "unknown";
}

export default function OccultPage() {
  const [status, setStatus] = useState<OccultDashboardStatus>(EMPTY_STATUS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [readingId, setReadingId] = useState("");
  const [reading, setReading] = useState<OccultReadingStatus | null>(null);
  const [readingBusy, setReadingBusy] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);
  const { toast, showToast } = useToast();
  const { setEnd } = usePageHeader();

  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await api.getOccultStatus());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    api
      .getOccultStatus()
      .then((nextStatus) => {
        if (active) setStatus(nextStatus);
      })
      .catch((caught) => {
        if (active) {
          setError(caught instanceof Error ? caught.message : String(caught));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useLayoutEffect(() => {
    setEnd(
      <Button outlined onClick={() => void loadStatus()} disabled={loading}>
        <RefreshCw className={`mr-2 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
        Refresh
      </Button>,
    );
    return () => setEnd(null);
  }, [loadStatus, loading, setEnd]);

  const runReadingAction = async (
    action: "inspect" | "resume" | "cancel",
  ) => {
    const id = readingId.trim();
    if (!id) {
      showToast("Enter a reading ID first.", "error");
      return;
    }
    setReadingBusy(true);
    try {
      const result =
        action === "inspect"
          ? await api.getOccultReading(id)
          : await api.controlOccultReading(id, action);
      setReading(result);
      showToast(
        action === "inspect" ? "Reading loaded." : `Reading ${action} requested.`,
        "success",
      );
    } catch (caught) {
      showToast(
        caught instanceof Error ? caught.message : String(caught),
        "error",
      );
    } finally {
      setReadingBusy(false);
      setCancelOpen(false);
    }
  };

  const summaryCards: Array<{
    label: string;
    count: number;
    icon: typeof Users;
  }> = [
    { label: "Major Arcana", count: status.agents.length, icon: Users },
    { label: "Minor Arcana", count: status.routes.length, icon: Route },
    { label: "Decks", count: status.decks.length, icon: Layers3 },
    { label: "Pairings", count: status.pairings.length, icon: ShieldCheck },
  ];

  if (loading && !status.connected) {
    return (
      <div className="flex min-h-[18rem] items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="space-y-5 pb-10">
      <Toast toast={toast} />
      <ConfirmDialog
        open={cancelOpen}
        title="Cancel reading?"
        description="This sends a real cancellation request to the Occult runtime. Completed nodes remain recorded."
        confirmLabel="Cancel reading"
        destructive
        loading={readingBusy}
        onCancel={() => setCancelOpen(false)}
        onConfirm={() => void runReadingAction("cancel")}
      />

      <section className="flex flex-col gap-3 border-b border-border pb-5 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2 text-primary">
            <Sparkles className="h-5 w-5" />
            <span className="font-courier text-[11px] uppercase tracking-[0.2em]">
              Occult System
            </span>
          </div>
          <h1 className="font-expanded text-2xl font-bold uppercase tracking-[0.06em]">
            Arcana control room
          </h1>
          <p className="mt-2 max-w-2xl font-mondwest text-sm text-muted-foreground">
            Inspect Major Arcana agents, available model routes, decks, pairings,
            and bounded Council readings. Router credentials stay on the server.
          </p>
        </div>
        <span
          className={`w-fit border px-3 py-1 font-courier text-[11px] uppercase tracking-wider ${statusTone(status.connected)}`}
        >
          {status.connected ? "Connected" : "Not connected"}
        </span>
      </section>

      {error && (
        <div role="alert" className="border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {!status.enabled || !status.configured || !status.connected ? (
        <Card>
          <CardHeader>
            <CardTitle>Finish Occult setup</CardTitle>
            <CardDescription>
              The dashboard remains inert until both the feature gate and a
              router-issued virtual token are present.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="grid gap-2 sm:grid-cols-2">
              <div className="border border-border p-3">
                <div className="font-courier text-xs uppercase tracking-wider">
                  Feature gate
                </div>
                <div className="mt-1 text-muted-foreground">
                  {status.enabled ? "Enabled" : "Set occult.enabled to true."}
                </div>
              </div>
              <div className="border border-border p-3">
                <div className="font-courier text-xs uppercase tracking-wider">
                  Virtual token
                </div>
                <div className="mt-1 text-muted-foreground">
                  {status.configured
                    ? "Configured"
                    : "Set OCCULT_API_KEY in the Hermes environment."}
                </div>
              </div>
            </div>
            {status.error && (
              <p role="alert" className="text-destructive">
                {status.error}
              </p>
            )}
          </CardContent>
        </Card>
      ) : (
        <>
          <section
            aria-label="Occult registry summary"
            className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
          >
            {summaryCards.map(({ label, count, icon: Icon }) => (
              <Card key={label}>
                <CardContent className="flex items-center justify-between p-4">
                  <div>
                    <div className="font-courier text-[11px] uppercase tracking-wider text-muted-foreground">
                      {label}
                    </div>
                    <div className="mt-1 text-2xl font-semibold">{count}</div>
                  </div>
                  <Icon className="h-5 w-5 text-primary" />
                </CardContent>
              </Card>
            ))}
          </section>

          <section className="grid gap-4 xl:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Major Arcana</CardTitle>
                <CardDescription>Agents allowed by the active virtual token.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-2">
                {status.agents.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No agents available.</p>
                ) : (
                  status.agents.map((agent) => (
                    <div key={agent.agent_id} className="border border-border p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="font-medium">{agent.name}</div>
                          <div className="mt-1 break-all font-courier text-[11px] text-muted-foreground">
                            {agent.agent_id}
                          </div>
                        </div>
                        {agent.arcana_number !== undefined && (
                          <span className="border border-border px-2 py-0.5 font-courier text-xs">
                            {agent.arcana_number}
                          </span>
                        )}
                      </div>
                    </div>
                  ))
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Minor Arcana routes</CardTitle>
                <CardDescription>
                  Provider and model details are shown without credential data.
                </CardDescription>
              </CardHeader>
              <CardContent className="max-h-[25rem] space-y-2 overflow-auto">
                {status.routes.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No routes available.</p>
                ) : (
                  status.routes.map((route) => (
                    <div key={route.card_id} className="border border-border p-3">
                      <div className="break-all font-courier text-xs">{route.card_id}</div>
                      <div className="mt-2 flex flex-wrap gap-1.5 text-[10px] uppercase tracking-wider">
                        {route.local && <span className="border border-border px-1.5 py-0.5">Local</span>}
                        {route.free && <span className="border border-success/30 px-1.5 py-0.5 text-success">Free</span>}
                        {route.trust_state && <span className="border border-border px-1.5 py-0.5">{route.trust_state}</span>}
                      </div>
                      {(route.provider_id || route.model_id) && (
                        <div className="mt-2 text-xs text-muted-foreground">
                          {[route.provider_id, route.model_id].filter(Boolean).join(" / ")}
                        </div>
                      )}
                    </div>
                  ))
                )}
              </CardContent>
            </Card>
          </section>

          <section className="grid gap-4 xl:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Decks</CardTitle>
                <CardDescription>Active operating configurations.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-2">
                {status.decks.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No decks installed.</p>
                ) : (
                  status.decks.map((deck) => (
                    <div key={deck.deck_id} className="flex items-center justify-between gap-3 border border-border p-3">
                      <div>
                        <div className="font-courier text-xs">{deck.deck_id}</div>
                        <div className="mt-1 text-xs text-muted-foreground">
                          Version {deck.version ?? "unknown"}
                        </div>
                      </div>
                      <span className="text-xs text-success">
                        {deck.active ? "Active" : "Available"}
                      </span>
                    </div>
                  ))
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Pairings</CardTitle>
                <CardDescription>Compatible agent and route combinations.</CardDescription>
              </CardHeader>
              <CardContent className="max-h-[20rem] space-y-2 overflow-auto">
                {status.pairings.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No pairings available.</p>
                ) : (
                  status.pairings.map((pairing, index) => (
                    <div
                      key={`${pairing.agent_id}:${pairing.card_id ?? index}`}
                      className="border border-border p-3 text-xs"
                    >
                      <div className="font-medium">{pairing.agent_id}</div>
                      <div className="mt-1 break-all font-courier text-muted-foreground">
                        {pairing.card_id ?? pairing.model_id ?? "Compatible route"}
                      </div>
                    </div>
                  ))
                )}
              </CardContent>
            </Card>
          </section>

          <Card>
            <CardHeader>
              <CardTitle>Council reading control</CardTitle>
              <CardDescription>
                Inspect, resume, or cancel one known reading. This does not
                replace the Hermes chat transcript or composer.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex flex-col gap-2 sm:flex-row">
                <label className="sr-only" htmlFor="occult-reading-id">
                  Reading ID
                </label>
                <Input
                  id="occult-reading-id"
                  value={readingId}
                  onChange={(event) => setReadingId(event.target.value)}
                  placeholder="reading ID"
                  autoComplete="off"
                />
                <div className="flex flex-wrap gap-2">
                  <Button outlined disabled={readingBusy} onClick={() => void runReadingAction("inspect")}>
                    <BookOpen className="mr-2 h-3.5 w-3.5" />
                    Inspect
                  </Button>
                  <Button outlined disabled={readingBusy} onClick={() => void runReadingAction("resume")}>
                    <Play className="mr-2 h-3.5 w-3.5" />
                    Resume
                  </Button>
                  <Button destructive disabled={readingBusy} onClick={() => setCancelOpen(true)}>
                    <Square className="mr-2 h-3.5 w-3.5" />
                    Cancel
                  </Button>
                </div>
              </div>

              {reading && (
                <div aria-live="polite" className="border border-border bg-background/30 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="font-courier text-xs uppercase tracking-wider">
                      {String(reading.reading_id ?? readingId)}
                    </div>
                    <span className="border border-border px-2 py-0.5 text-xs">
                      {labelForState(reading.state)}
                    </span>
                  </div>
                  {reading.spread_id && (
                    <p className="mt-2 text-xs text-muted-foreground">
                      Spread: {reading.spread_id}
                    </p>
                  )}
                  {Array.isArray(reading.nodes) && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      Nodes: {reading.nodes.length}
                    </p>
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
