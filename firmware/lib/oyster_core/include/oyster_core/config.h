// Compile-time limits. Engineering defaults from the engineering plan: limits to
// measure and tune, not claims about demonstrated performance. Every bounded collection also
// keeps an overflow counter so a partial result never looks complete.
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster::cfg {

// In-memory views
constexpr size_t kMaxApObservations = 32;
constexpr size_t kMaxBleAdverts = 64;
constexpr size_t kMaxGattEntriesPerPage = 64;
constexpr size_t kMaxBleConnections = 1;
constexpr size_t kMaxEnrolledTargets = 8;

// Field bounds
constexpr size_t kMaxIdLen = 40;          // characters after the prefix: [A-Za-z0-9_-]
constexpr size_t kIdBufLen = 48;          // prefix + id + NUL
constexpr size_t kMaxSsidBytes = 32;
constexpr size_t kMaxBleNameBytes = 29;
constexpr size_t kMaxMfgDataBytes = 24;
constexpr size_t kMaxSvcUuidsPerAdv = 4;
constexpr size_t kUuidStrLen = 36;        // without NUL
constexpr size_t kMaxRoomLabelLen = 32;
constexpr size_t kMaxQuestionLen = 240;

// Transport and storage
constexpr size_t kMaxApiPageBytes = 4096;
constexpr size_t kMaxEventBytes = 2048;           // one serialized JSONL event line, newline included
constexpr size_t kStreamBufBytes = 2048;          // inside the 1-4 KiB streaming budget
constexpr size_t kMaxDiagObjectBytes = 65536;
constexpr size_t kMaxJobArgsBytes = 512;
constexpr size_t kVolatileEventRingBytes = 8192;  // evidence kept in RAM when no SD card is present
constexpr size_t kObservationsPerEventPage = 8;   // AP / advert observations per evidence event
constexpr size_t kMaxResultEventIds = 16;

// Health goals: measured and reported, never asserted
constexpr size_t kMinFreeHeapGoal = 64 * 1024;
constexpr size_t kMinLargestBlockGoal = 32 * 1024;

// Timing defaults (milliseconds unless stated)
constexpr uint32_t kWifiScanMsPerChannel = 300;
constexpr uint32_t kWifiScanMaxMs = 30000;
constexpr uint32_t kBleDiscoveryMs = 6000;
constexpr uint32_t kBleDiscoveryMaxMs = 30000;
constexpr uint32_t kBleConnectTimeoutMs = 10000;
constexpr uint32_t kBackendPollMs = 4000;
constexpr uint32_t kTlsHandshakeTimeoutS = 15;
constexpr uint32_t kHttpTimeoutMs = 10000;
constexpr uint32_t kLocalConfirmTimeoutMs = 120000;
constexpr uint32_t kTelemetryPeriodMs = 60000;
constexpr uint32_t kUiCancelAckMs = 1000;         // target for the UI to acknowledge a cancel
constexpr uint32_t kMaxJobDurationMs = 600000;

}  // namespace oyster::cfg
