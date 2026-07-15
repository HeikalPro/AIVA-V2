from pydantic import BaseModel


class ComponentStatus(BaseModel):
    status: str  # up | down | not_configured | unknown
    latency_ms: float | None = None
    detail: str | None = None


class RedisStatus(ComponentStatus):
    queue_name: str | None = None
    queue_depth: int | None = None


class ResourceStats(BaseModel):
    cpu_percent: float | None = None
    memory_percent: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    detail: str | None = None


class TrafficStats(BaseModel):
    window_minutes: int
    request_count: int | None = None
    error_count: int | None = None
    error_rate: float | None = None
    requests_per_minute: float | None = None
    avg_latency_ms: float | None = None


class SystemHealthOut(BaseModel):
    status: str  # ok | degraded | down
    generated_at: str
    database: ComponentStatus
    redis: RedisStatus
    resources: ResourceStats
    sse_connections: int
    traffic: TrafficStats


# ---- Detailed System Resources snapshot ----


class PlatformInfo(BaseModel):
    app_name: str = "AIVA"
    hostname: str | None = None
    os: str | None = None
    os_detail: str | None = None
    python_version: str | None = None


class CpuTimes(BaseModel):
    user: float | None = None
    system: float | None = None
    idle: float | None = None
    iowait: float | None = None


class CpuInfo(BaseModel):
    percent: float | None = None
    cores: int | None = None
    freq_mhz: float | None = None
    per_core: list[float | None] = []
    times: CpuTimes = CpuTimes()
    load_avg: list[float] | None = None


class MemoryInfo(BaseModel):
    total_mb: float | None = None
    used_mb: float | None = None
    available_mb: float | None = None
    percent: float | None = None
    cached_mb: float | None = None
    buffers_mb: float | None = None


class SwapInfo(BaseModel):
    total_mb: float | None = None
    used_mb: float | None = None
    percent: float | None = None


class DiskInfo(BaseModel):
    total_gb: float | None = None
    used_gb: float | None = None
    free_gb: float | None = None
    percent: float | None = None


class DiskIoInfo(BaseModel):
    read_mbps: float | None = None
    write_mbps: float | None = None
    read_iops: float | None = None
    write_iops: float | None = None
    read_total: int | None = None
    write_total: int | None = None


class NetworkInfo(BaseModel):
    sent_total_gb: float | None = None
    recv_total_gb: float | None = None
    packets_sent: int | None = None
    packets_recv: int | None = None
    errin: int | None = None
    errout: int | None = None
    dropin: int | None = None
    dropout: int | None = None
    sent_mbps: float | None = None
    recv_mbps: float | None = None


class ProcessInfo(BaseModel):
    pid: int | None = None
    memory_mb: float | None = None
    num_threads: int | None = None
    cpu_percent: float | None = None


class ComponentHealth(BaseModel):
    key: str
    label: str
    status: str  # up | down | degraded | not_configured | unknown
    latency_ms: float | None = None
    detail: str | None = None
    info: str | None = None


class SystemComponentsOut(BaseModel):
    generated_at: str
    components: list[ComponentHealth]


class SystemResourcesOut(BaseModel):
    generated_at: str
    platform: PlatformInfo
    cpu: CpuInfo
    memory: MemoryInfo
    swap: SwapInfo | None = None
    disk: DiskInfo
    disk_io: DiskIoInfo
    network: NetworkInfo
    process: ProcessInfo
    uptime_seconds: int | None = None
    boot_time: str | None = None
    detail: str | None = None
