const getNumeric = (signal) => {
  if (signal === undefined || signal === null) return null;
  if (typeof signal === 'number') return Number.isFinite(signal) ? signal : null;
  if (typeof signal === 'object') {
    if (typeof signal.raw === 'number') return signal.raw;
    if (typeof signal.value === 'number') return signal.value;
  }
  const parsed = Number(signal);
  return Number.isFinite(parsed) ? parsed : null;
};

const bitValue = (signal) => {
  const numeric = getNumeric(signal);
  return numeric === null ? null : numeric !== 0;
};

export const SET_VCU_CONFIG_ID = 0x800000CF;

export const clampNumber = (value, min, max, fallback = min) => {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return fallback;
  return Math.min(max, Math.max(min, numeric));
};

export const createDefaultVcuConfig = () => ({
  maxTorqueNm: 0,
  maxSpeedMph: 0,
  motorDirection: 1,
  regenEnabled: false,
  regenSocGateEnabled: false,
  echoDaq: false,
  ignoreRtdSwitch: false,
  ignoreRtdBrakes: false,
  useApps1Only: false,
  useApps2Only: false,
  ignoreAppsErrs: false,
  ignoreBseErrs: false,
  ignoreSdc: false,
  ignoreBrakePlausibility: false,
  alwaysGreen: false,
  wheelDiameterIn: 18,
  tcEnabled: false,
  tcTargetSlip: 1,
  tcKp: 0,
  tcKi: 0,
  tcKd: 0,
  tcMinFrontRpm: 0,
  powerLimitEnabled: false,
  powerCapKw: 50,
  regenMaxTorqueNm: 0,
  regenMinTorqueNm: 0,
  regenMinBseRearPsi: 0,
  regenMinBseFrontPsi: 0,
  regenMaxBseRearPsi: 0,
  regenMaxBseFrontPsi: 0,
  regenMinSpeedRpm: 0,
  regenMaxSocPct: 100,
  regenMaxAppsPct: 100,
  regenRyderMu: 0,
  regenStrategy: 0,
  launchTorqueOfftheline: 100,
  launchTorqueInit: 150,
  launchTorqueFinal: 183,
  launchCommand: 0,
});

export const getVcuConfigFromSignals = (getSignal) => ({
  maxTorqueNm: getNumeric(getSignal('VCU_Max_Torque')) ?? 0,
  maxSpeedMph: getNumeric(getSignal('VCU_Max_Speed_MPH')) ?? 0,
  motorDirection: getNumeric(getSignal('VCU_Motor_Direction')) ?? 1,
  regenEnabled: bitValue(getSignal('VCU_Regen_Enabled')) ?? false,
  regenSocGateEnabled: bitValue(getSignal('VCU_Regen_SOC_Gate_Enabled')) ?? false,
  echoDaq: bitValue(getSignal('VCU_ECHO_DAQ')) ?? false,
  ignoreRtdSwitch: bitValue(getSignal('VCU_Ignore_RTD_Switch')) ?? false,
  ignoreRtdBrakes: bitValue(getSignal('VCU_Ignore_RTD_Brakes')) ?? false,
  useApps1Only: bitValue(getSignal('VCU_Use_APPS1_Only')) ?? false,
  useApps2Only: bitValue(getSignal('VCU_Use_APPS2_Only')) ?? false,
  ignoreAppsErrs: bitValue(getSignal('VCU_Ignore_APPS_Errs')) ?? false,
  ignoreBseErrs: bitValue(getSignal('VCU_Ignore_BSE_Errs')) ?? false,
  ignoreSdc: bitValue(getSignal('VCU_Ignore_SDC')) ?? false,
  ignoreBrakePlausibility: bitValue(getSignal('VCU_Ignore_Brake_Plausibility')) ?? false,
  alwaysGreen: bitValue(getSignal('VCU_Always_Green')) ?? false,
  wheelDiameterIn: getNumeric(getSignal('VCU_Wheel_Diameter')) ?? 18,
  tcEnabled: bitValue(getSignal('VCU_TC_Enabled')) ?? false,
  tcTargetSlip: getNumeric(getSignal('VCU_TC_Target_Slip')) ?? 1,
  tcKp: getNumeric(getSignal('VCU_TC_Kp')) ?? 0,
  tcKi: getNumeric(getSignal('VCU_TC_Ki')) ?? 0,
  tcKd: getNumeric(getSignal('VCU_TC_Kd')) ?? 0,
  tcMinFrontRpm: getNumeric(getSignal('VCU_TC_Min_Front_RPM')) ?? 0,
  powerLimitEnabled: bitValue(getSignal('VCU_Power_Limit_Enabled')) ?? false,
  powerCapKw: getNumeric(getSignal('VCU_Power_Cap_kW')) ?? 50,
  regenMaxTorqueNm: getNumeric(getSignal('VCU_Regen_Max_Torque')) ?? 0,
  regenMinTorqueNm: getNumeric(getSignal('VCU_Regen_Min_Torque')) ?? 0,
  regenMinBseRearPsi: getNumeric(getSignal('VCU_Regen_Min_BSE_Rear_PSI')) ?? 0,
  regenMinBseFrontPsi: getNumeric(getSignal('VCU_Regen_Min_BSE_Front_PSI')) ?? 0,
  regenMaxBseRearPsi: getNumeric(getSignal('VCU_Regen_Max_BSE_Rear_PSI')) ?? 0,
  regenMaxBseFrontPsi: getNumeric(getSignal('VCU_Regen_Max_BSE_Front_PSI')) ?? 0,
  regenMinSpeedRpm: getNumeric(getSignal('VCU_Regen_Min_Speed')) ?? 0,
  regenMaxSocPct: getNumeric(getSignal('VCU_Regen_Max_SOC')) ?? 100,
  regenMaxAppsPct: getNumeric(getSignal('VCU_Regen_Max_APPS')) ?? 100,
  regenRyderMu: getNumeric(getSignal('VCU_Regen_Ryder_Mu')) ?? 0,
  regenStrategy: getNumeric(getSignal('VCU_Regen_Strategy')) ?? 0,
  launchTorqueOfftheline: getNumeric(getSignal('VCU_Launch_Torque_Offtheline')) ?? 100,
  launchTorqueInit: getNumeric(getSignal('VCU_Launch_Torque_Init')) ?? 150,
  launchTorqueFinal: getNumeric(getSignal('VCU_Launch_Torque_Final')) ?? 183,
  launchCommand: 0,
});

export const buildVcuConfigFrame = (mux, config) => {
  const bytes = new Uint8Array(8);
  bytes[0] = mux;
  const view = new DataView(bytes.buffer);

  if (mux === 0) view.setUint16(1, Math.round(clampNumber(config.maxTorqueNm, 0, 230, 0)), true);
  if (mux === 50) view.setUint16(1, Math.round(clampNumber(config.maxSpeedMph, 0, 419, 0)), true);
  if (mux === 1) bytes[1] = Number(config.motorDirection) ? 1 : 0;
  if (mux === 2) bytes[1] = config.regenEnabled ? 1 : 0;
  if (mux === 3) {
    let packed = 0;
    if (config.echoDaq) packed |= 1 << 0;
    if (config.ignoreRtdSwitch) packed |= 1 << 1;
    if (config.ignoreRtdBrakes) packed |= 1 << 2;
    if (config.useApps1Only) packed |= 1 << 3;
    if (config.useApps2Only) packed |= 1 << 4;
    if (config.ignoreAppsErrs) packed |= 1 << 5;
    if (config.ignoreBseErrs) packed |= 1 << 6;
    if (config.ignoreSdc) packed |= 1 << 7;
    if (config.ignoreBrakePlausibility) packed |= 1 << 8;
    if (config.alwaysGreen) packed |= 1 << 9;
    bytes[1] = packed & 0xFF;
    bytes[2] = (packed >> 8) & 0xFF;
  }
  if (mux === 4) view.setUint16(1, Math.round(clampNumber(config.wheelDiameterIn, 8, 30, 18)), true);
  if (mux === 5) bytes[1] = config.tcEnabled ? 1 : 0;
  if (mux === 6) view.setUint16(1, Math.round(clampNumber(config.tcTargetSlip, 1, 5, 1) * 1000), true);
  if (mux === 7) view.setUint16(1, Math.round(clampNumber(config.tcKp, 0, 32.767, 0) * 1000), true);
  if (mux === 8) view.setUint16(1, Math.round(clampNumber(config.tcKi, 0, 32.767, 0) * 1000), true);
  if (mux === 9) view.setUint16(1, Math.round(clampNumber(config.tcKd, 0, 32.767, 0) * 1000), true);
  if (mux === 10) view.setUint16(1, Math.round(clampNumber(config.tcMinFrontRpm, 0, 32767, 0)), true);
  if (mux === 11) bytes[1] = config.powerLimitEnabled ? 1 : 0;
  if (mux === 12) view.setUint16(1, Math.round(clampNumber(config.powerCapKw, 5, 100, 50)), true);
  if (mux === 13) view.setUint16(1, Math.round(clampNumber(config.regenMaxTorqueNm, 0, 230, 0)), true);
  if (mux === 14) view.setUint16(1, Math.round(clampNumber(config.regenMinTorqueNm, 0, 230, 0)), true);
  if (mux === 15) view.setUint16(1, Math.round(clampNumber(config.regenMinBseRearPsi, 0, 10000, 0)), true);
  if (mux === 16) view.setUint16(1, Math.round(clampNumber(config.regenMinBseFrontPsi, 0, 10000, 0)), true);
  if (mux === 17) view.setUint16(1, Math.round(clampNumber(config.regenMaxBseRearPsi, 0, 10000, 0)), true);
  if (mux === 18) view.setUint16(1, Math.round(clampNumber(config.regenMaxBseFrontPsi, 0, 10000, 0)), true);
  if (mux === 19) view.setUint16(1, Math.round(clampNumber(config.regenMinSpeedRpm, 0, 32767, 0)), true);
  if (mux === 20) view.setUint16(1, Math.round(clampNumber(config.regenMaxSocPct, 0, 100, 100)), true);
  if (mux === 21) bytes[1] = Math.round(clampNumber(config.regenStrategy, 0, 3, 0));
  if (mux === 22) bytes[1] = config.launchEnabled ? 1 : 0;
  if (mux === 23) view.setUint16(1, Math.round(clampNumber(config.launchEndRpm, 0, 32767, 0)), true);
  if (mux === 24) view.setUint16(1, Math.round(clampNumber(config.launchTimeoutMs, 0, 60000, 0)), true);
  if (mux === 25) view.setUint16(1, Math.round(clampNumber(config.launchMaxSlip, 1, 5, 1) * 1000), true);
  if (mux === 26) bytes[1] = Math.round(clampNumber(config.launchBestCurve, 0, 2, 0));
  if (mux === 27) bytes[1] = Math.round(clampNumber(config.launchActiveCurve, 0, 3, 3));
  if (mux === 28) view.setUint16(1, Math.round(clampNumber(config.launchRecommendedSlip, 1, 5, 1) * 1000), true);
  if (mux === 29) view.setUint16(1, Math.round(clampNumber(config.launchActualRpm0, 0, 32767, 0)), true);
  if (mux === 30) view.setUint16(1, Math.round(clampNumber(config.launchActualRpm1, 0, 32767, 1000)), true);
  if (mux === 31) view.setUint16(1, Math.round(clampNumber(config.launchActualRpm2, 0, 32767, 2000)), true);
  if (mux === 32) view.setUint16(1, Math.round(clampNumber(config.launchActualRpm3, 0, 32767, 4000)), true);
  if (mux === 33) view.setUint16(1, Math.round(clampNumber(config.launchActualRpm4, 0, 32767, 6000)), true);
  if (mux === 34) view.setUint16(1, Math.round(clampNumber(config.launchActualTorque0, 0, 230, 40)), true);
  if (mux === 35) view.setUint16(1, Math.round(clampNumber(config.launchActualTorque1, 0, 230, 50)), true);
  if (mux === 36) view.setUint16(1, Math.round(clampNumber(config.launchActualTorque2, 0, 230, 65)), true);
  if (mux === 37) view.setUint16(1, Math.round(clampNumber(config.launchActualTorque3, 0, 230, 80)), true);
  if (mux === 38) view.setUint16(1, Math.round(clampNumber(config.launchActualTorque4, 0, 230, 90)), true);
  if (mux === 39) bytes[1] = config.regenSocGateEnabled ? 1 : 0;
  if (mux === 40) view.setUint16(1, Math.round(clampNumber(config.regenMaxAppsPct, 0, 100, 100)), true);
  if (mux === 41) view.setUint16(1, Math.round(clampNumber(config.regenRyderMu, 0, 10, 0) * 1000), true);
  if (mux === 42) view.setUint16(1, Math.round(clampNumber(config.launchTorqueOfftheline, 0, 230, 100)), true);
  if (mux === 43) view.setUint16(1, Math.round(clampNumber(config.launchTorqueInit, 0, 230, 150)), true);
  if (mux === 44) view.setUint16(1, Math.round(clampNumber(config.launchTorqueFinal, 0, 230, 183)), true);
  if (mux === 240) view.setInt16(1, Math.round(clampNumber(config.launchCommand, 0, 2, 0)), true);

  return Array.from(bytes);
};