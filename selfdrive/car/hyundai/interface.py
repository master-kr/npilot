from cereal import car
from common.numpy_fast import interp
from panda import Panda
from openpilot.common.conversions import Conversions as CV
from openpilot.selfdrive.car.hyundai.hyundaicanfd import CanBus
from openpilot.selfdrive.car.hyundai import interface_community
from openpilot.selfdrive.car.hyundai.values import HyundaiFlags, CAR, DBC, CANFD_CAR, CAMERA_SCC_CAR, CANFD_RADAR_SCC_CAR, \
                                         CANFD_UNSUPPORTED_LONGITUDINAL_CAR, EV_CAR, HYBRID_CAR, LEGACY_SAFETY_MODE_CAR, \
                                         UNSUPPORTED_LONGITUDINAL_CAR, Buttons, CANFD_HDA2_CAR, CANFD_HDA2_ALT_GEARS
from openpilot.selfdrive.car.hyundai.radar_interface import RADAR_START_ADDR
from openpilot.selfdrive.car import create_button_events, get_safety_config
from openpilot.selfdrive.car.interfaces import CarInterfaceBase, ACCEL_MIN, ACCEL_MAX
from openpilot.selfdrive.car.disable_ecu import disable_ecu
from openpilot.selfdrive.controls.neokii.cruise_state_manager import is_radar_point
from openpilot.common.params import Params

Ecu = car.CarParams.Ecu
ButtonType = car.CarState.ButtonEvent.Type
EventName = car.CarEvent.EventName
ENABLE_BUTTONS = (Buttons.RES_ACCEL, Buttons.SET_DECEL, Buttons.CANCEL)
BUTTONS_DICT = {Buttons.RES_ACCEL: ButtonType.accelCruise, Buttons.SET_DECEL: ButtonType.decelCruise,
                Buttons.GAP_DIST: ButtonType.gapAdjustCruise, Buttons.CANCEL: ButtonType.cancel}


class CarInterface(CarInterfaceBase):
  @staticmethod
  def get_pid_accel_limits(CP, current_speed, cruise_speed):
    v_current_kph = current_speed * CV.MS_TO_KPH
    gas_max_bp = [10., 20., 50., 70., 130., 150.]
    gas_max_v = [ACCEL_MAX, 1.5, 1.0, 0.6, 0.2, 0.1]
    return ACCEL_MIN, interp(v_current_kph, gas_max_bp, gas_max_v)
  
  @staticmethod
  def _get_params(ret, candidate, fingerprint, car_fw, experimental_long, docs):
    ret.carName = "hyundai"
    ret.radarUnavailable = RADAR_START_ADDR not in fingerprint[1] or DBC[ret.carFingerprint]["radar"] is None

    # These cars have been put into dashcam only due to both a lack of users and test coverage.
    # These cars likely still work fine. Once a user confirms each car works and a test route is
    # added to selfdrive/car/tests/routes.py, we can remove it from this list.
    # FIXME: the Optima Hybrid 2017 uses a different SCC12 checksum
    ret.dashcamOnly = candidate in {CAR.KIA_OPTIMA_H, }

    hda2 = Ecu.adas in [fw.ecu for fw in car_fw] or candidate in CANFD_HDA2_CAR
    CAN = CanBus(None, hda2, fingerprint)

    if candidate in CANFD_CAR:
      # detect HDA2 with ADAS Driving ECU
      if hda2:
        ret.flags |= HyundaiFlags.CANFD_HDA2.value
        if 0x110 in fingerprint[CAN.CAM]:
          ret.flags |= HyundaiFlags.CANFD_HDA2_ALT_STEERING.value
        if candidate in CANFD_HDA2_ALT_GEARS:
          ret.flags |= HyundaiFlags.CANFD_ALT_GEARS.value
      else:
        # non-HDA2
        if 0x1cf not in fingerprint[CAN.ECAN]:
          ret.flags |= HyundaiFlags.CANFD_ALT_BUTTONS.value
        # ICE cars do not have 0x130; GEARS message on 0x40 or 0x70 instead
        if 0x130 not in fingerprint[CAN.ECAN]:
          if 0x40 not in fingerprint[CAN.ECAN]:
            ret.flags |= HyundaiFlags.CANFD_ALT_GEARS_2.value
          else:
            ret.flags |= HyundaiFlags.CANFD_ALT_GEARS.value
        if candidate not in CANFD_RADAR_SCC_CAR:
          ret.flags |= HyundaiFlags.CANFD_CAMERA_SCC.value
    else:
      # Send LFA message on cars with HDA
      if 0x485 in fingerprint[2]:
        ret.flags |= HyundaiFlags.SEND_LFA.value

      # These cars use the FCA11 message for the AEB and FCW signals, all others use SCC12
      if 0x38d in fingerprint[0] or 0x38d in fingerprint[2]:
        ret.flags |= HyundaiFlags.USE_FCA.value

    ret.steerActuatorDelay = 0.1  # Default delay
    ret.steerLimitTimer = 0.4
    CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    if candidate in (CAR.AZERA_6TH_GEN, CAR.AZERA_HEV_6TH_GEN):
      ret.mass = 1600. if candidate == CAR.AZERA_6TH_GEN else 1675.  # ICE is ~average of 2.5L and 3.5L
      ret.wheelbase = 2.885
      ret.steerRatio = 14.5
    elif candidate in (CAR.SANTA_FE, CAR.SANTA_FE_2022, CAR.SANTA_FE_HEV_2022, CAR.SANTA_FE_PHEV_2022):
      ret.mass = 3982. * CV.LB_TO_KG
      ret.wheelbase = 2.766
      # Values from optimizer
      ret.steerRatio = 16.55  # 13.8 is spec end-to-end
      ret.tireStiffnessFactor = 0.82
    elif candidate in (CAR.SONATA, CAR.SONATA_HYBRID):
      ret.mass = 1513.
      ret.wheelbase = 2.84
      ret.steerRatio = 13.27 * 1.15   # 15% higher at the center seems reasonable
      ret.tireStiffnessFactor = 0.65
    elif candidate == CAR.SONATA_LF:
      ret.mass = 1536.
      ret.wheelbase = 2.804
      ret.steerRatio = 13.27 * 1.15   # 15% higher at the center seems reasonable
    elif candidate == CAR.PALISADE:
      ret.mass = 1999.
      ret.wheelbase = 2.90
      ret.steerRatio = 15.6 * 1.15
      ret.tireStiffnessFactor = 0.63
    elif candidate in (CAR.ELANTRA, CAR.ELANTRA_GT_I30):
      ret.mass = 1275.
      ret.wheelbase = 2.7
      ret.steerRatio = 15.4            # 14 is Stock | Settled Params Learner values are steerRatio: 15.401566348670535
      ret.tireStiffnessFactor = 0.385    # stiffnessFactor settled on 1.0081302973865127
      ret.minSteerSpeed = 32 * CV.MPH_TO_MS
    elif candidate == CAR.ELANTRA_2021:
      ret.mass = 2800. * CV.LB_TO_KG
      ret.wheelbase = 2.72
      ret.steerRatio = 12.9
      ret.tireStiffnessFactor = 0.65
    elif candidate == CAR.ELANTRA_HEV_2021:
      ret.mass = 3017. * CV.LB_TO_KG
      ret.wheelbase = 2.72
      ret.steerRatio = 12.9
      ret.tireStiffnessFactor = 0.65
    elif candidate == CAR.HYUNDAI_GENESIS:
      ret.mass = 2060.
      ret.wheelbase = 3.01
      ret.steerRatio = 16.5
      ret.minSteerSpeed = 60 * CV.KPH_TO_MS
    elif candidate in (CAR.KONA, CAR.KONA_EV, CAR.KONA_HEV, CAR.KONA_EV_2022, CAR.KONA_EV_2ND_GEN):
      ret.mass = {CAR.KONA_EV: 1685., CAR.KONA_HEV: 1425., CAR.KONA_EV_2022: 1743., CAR.KONA_EV_2ND_GEN: 1740.}.get(candidate, 1275.)
      ret.wheelbase = {CAR.KONA_EV_2ND_GEN: 2.66, }.get(candidate, 2.6)
      ret.steerRatio = {CAR.KONA_EV_2ND_GEN: 13.6, }.get(candidate, 13.42)  # Spec
      ret.tireStiffnessFactor = 0.385
    elif candidate in (CAR.IONIQ, CAR.IONIQ_EV_LTD, CAR.IONIQ_PHEV_2019, CAR.IONIQ_HEV_2022, CAR.IONIQ_EV_2020, CAR.IONIQ_PHEV):
      ret.mass = 1490.  # weight per hyundai site https://www.hyundaiusa.com/ioniq-electric/specifications.aspx
      ret.wheelbase = 2.7
      ret.steerRatio = 13.73  # Spec
      ret.tireStiffnessFactor = 0.385
      if candidate in (CAR.IONIQ, CAR.IONIQ_EV_LTD, CAR.IONIQ_PHEV_2019):
        ret.minSteerSpeed = 32 * CV.MPH_TO_MS
    elif candidate in (CAR.IONIQ_5, CAR.IONIQ_6):
      ret.mass = 1948
      ret.wheelbase = 2.97
      ret.steerRatio = 14.26
      ret.tireStiffnessFactor = 0.65
    elif candidate == CAR.VELOSTER:
      ret.mass = 2917. * CV.LB_TO_KG
      ret.wheelbase = 2.80
      ret.steerRatio = 13.75 * 1.15
      ret.tireStiffnessFactor = 0.5
    elif candidate == CAR.TUCSON:
      ret.mass = 3520. * CV.LB_TO_KG
      ret.wheelbase = 2.67
      ret.steerRatio = 14.00 * 1.15
      ret.tireStiffnessFactor = 0.385
    elif candidate in (CAR.TUCSON_4TH_GEN, CAR.TUCSON_HYBRID_4TH_GEN):
      ret.mass = 1630.  # average
      ret.wheelbase = 2.756
      ret.steerRatio = 16.
      ret.tireStiffnessFactor = 0.385
    elif candidate == CAR.SANTA_CRUZ_1ST_GEN:
      ret.mass = 1870.  # weight from Limited trim - the only supported trim
      ret.wheelbase = 3.000
      # steering ratio according to Hyundai News https://www.hyundainews.com/assets/documents/original/48035-2022SantaCruzProductGuideSpecsv2081521.pdf
      ret.steerRatio = 14.2
    elif candidate == CAR.CUSTIN_1ST_GEN:
      ret.mass = 1690.  # from https://www.hyundai-motor.com.tw/clicktobuy/custin#spec_0
      ret.wheelbase = 3.055
      ret.steerRatio = 17.0  # from learner
    elif candidate == CAR.STARIA:
      ret.mass = 2280.  
      ret.wheelbase = 3.275
      ret.steerRatio = 14.2
    elif candidate == CAR.CASPER:
      ret.mass = 985.
      ret.wheelbase = 2.40
      ret.steerRatio = 14.2
    elif candidate == CAR.GRANDEUR_GN7:
      ret.mass = 1620.
      ret.wheelbase = 2.895
      ret.steerRatio = 14.2
    elif candidate == CAR.GRANDEUR_GN7_HYBRID:
      ret.mass = 1700.
      ret.wheelbase = 2.895
      ret.steerRatio = 14.2
    elif candidate == CAR.IONIQ_5N:
      ret.mass = 2200
      ret.wheelbase = 3.0
      ret.steerRatio = 14.26
      ret.tireStiffnessFactor = 0.65

    # Kia
    elif candidate == CAR.KIA_SORENTO:
      ret.mass = 1985.
      ret.wheelbase = 2.78
      ret.steerRatio = 14.4 * 1.1   # 10% higher at the center seems reasonable
    elif candidate in (CAR.KIA_NIRO_EV, CAR.KIA_NIRO_EV_2ND_GEN, CAR.KIA_NIRO_PHEV, CAR.KIA_NIRO_HEV_2021, CAR.KIA_NIRO_HEV_2ND_GEN):
      ret.mass = 3543. * CV.LB_TO_KG  # average of all the cars
      ret.wheelbase = 2.7
      ret.steerRatio = 13.6  # average of all the cars
      ret.tireStiffnessFactor = 0.385
      if candidate == CAR.KIA_NIRO_PHEV:
        ret.minSteerSpeed = 32 * CV.MPH_TO_MS
    elif candidate == CAR.KIA_SELTOS:
      ret.mass = 1337.
      ret.wheelbase = 2.63
      ret.steerRatio = 14.56
    elif candidate == CAR.KIA_SPORTAGE_5TH_GEN:
      ret.mass = 1700.  # weight from SX and above trims, average of FWD and AWD versions
      ret.wheelbase = 2.756
      ret.steerRatio = 13.6  # steering ratio according to Kia News https://www.kiamedia.com/us/en/models/sportage/2023/specifications
    elif candidate in (CAR.KIA_OPTIMA_G4, CAR.KIA_OPTIMA_G4_FL, CAR.KIA_OPTIMA_H, CAR.KIA_OPTIMA_H_G4_FL):
      ret.mass = 3558. * CV.LB_TO_KG
      ret.wheelbase = 2.80
      ret.steerRatio = 13.75
      ret.tireStiffnessFactor = 0.5
      if candidate == CAR.KIA_OPTIMA_G4:
        ret.minSteerSpeed = 32 * CV.MPH_TO_MS
    elif candidate in (CAR.KIA_STINGER, CAR.KIA_STINGER_2022):
      ret.mass = 1825.
      ret.wheelbase = 2.78
      ret.steerRatio = 14.4 * 1.15   # 15% higher at the center seems reasonable
    elif candidate == CAR.KIA_FORTE:
      ret.mass = 2878. * CV.LB_TO_KG
      ret.wheelbase = 2.80
      ret.steerRatio = 13.75
      ret.tireStiffnessFactor = 0.5
    elif candidate == CAR.KIA_CEED:
      ret.mass = 1450.
      ret.wheelbase = 2.65
      ret.steerRatio = 13.75
      ret.tireStiffnessFactor = 0.5
    elif candidate in (CAR.KIA_K5_2021, CAR.KIA_K5_HEV_2020):
      ret.mass = 3381. * CV.LB_TO_KG
      ret.wheelbase = 2.85
      ret.steerRatio = 13.27  # 2021 Kia K5 Steering Ratio (all trims)
      ret.tireStiffnessFactor = 0.5
    elif candidate in (CAR.KIA_K5_2024, CAR.KIA_K5_HEV_2024):
      ret.mass = 3381. * CV.LB_TO_KG
      ret.wheelbase = 2.85
      ret.steerRatio = 13.27  # 2021 Kia K5 Steering Ratio (all trims)
      ret.tireStiffnessFactor = 0.5
    elif candidate == CAR.KIA_EV6:
      ret.mass = 2055
      ret.wheelbase = 2.9
      ret.steerRatio = 16.
      ret.tireStiffnessFactor = 0.65
    elif candidate == CAR.KIA_SPORTAGE_HYBRID_5TH_GEN:
      ret.mass = 1767.  # SX Prestige trim support only
      ret.wheelbase = 2.756
      ret.steerRatio = 13.6
    elif candidate in (CAR.KIA_SORENTO_4TH_GEN, CAR.KIA_SORENTO_HEV_4TH_GEN, CAR.KIA_SORENTO_PHEV_4TH_GEN):
      ret.wheelbase = 2.81
      ret.steerRatio = 13.5  # average of the platforms
      if candidate == CAR.KIA_SORENTO_4TH_GEN:
        ret.mass = 3957 * CV.LB_TO_KG
      elif candidate == CAR.KIA_SORENTO_HEV_4TH_GEN:
        ret.mass = 4255 * CV.LB_TO_KG
      else:
        ret.mass = 4537 * CV.LB_TO_KG
    elif candidate == CAR.KIA_CARNIVAL_4TH_GEN:
      ret.mass = 2087.
      ret.wheelbase = 3.09
      ret.steerRatio = 14.23
    elif candidate == CAR.KIA_K8_HEV_1ST_GEN:
      ret.mass = 1630.  # https://carprices.ae/brands/kia/2023/k8/1.6-turbo-hybrid
      ret.wheelbase = 2.895
      ret.steerRatio = 13.27  # guesstimate from K5 platform
    elif candidate == CAR.KIA_K8:
      ret.mass = 1540.
      ret.wheelbase = 2.895
      ret.steerRatio = 13.27  # guesstimate from K5 platform

    # Genesis
    elif candidate == CAR.GENESIS_GV60_EV_1ST_GEN:
      ret.mass = 2205
      ret.wheelbase = 2.9
      # https://www.motor1.com/reviews/586376/2023-genesis-gv60-first-drive/#:~:text=Relative%20to%20the%20related%20Ioniq,5%2FEV6%27s%2014.3%3A1.
      ret.steerRatio = 12.6
    elif candidate == CAR.GENESIS_G70:
      ret.steerActuatorDelay = 0.1
      ret.mass = 1640.0
      ret.wheelbase = 2.84
      ret.steerRatio = 13.56
    elif candidate == CAR.GENESIS_G70_2020:
      ret.mass = 3673.0 * CV.LB_TO_KG
      ret.wheelbase = 2.83
      ret.steerRatio = 12.9
    elif candidate == CAR.GENESIS_GV70_1ST_GEN:
      ret.mass = 1950.
      ret.wheelbase = 2.87
      ret.steerRatio = 14.6
    elif candidate == CAR.GENESIS_G80:
      ret.mass = 2060.
      ret.wheelbase = 3.01
      ret.steerRatio = 16.5
    elif candidate == CAR.GENESIS_G90:
      ret.mass = 2200.
      ret.wheelbase = 3.15
      ret.steerRatio = 12.069
    elif candidate == CAR.GENESIS_GV80:
      ret.mass = 2258.
      ret.wheelbase = 2.95
      ret.steerRatio = 14.14
    elif candidate == CAR.GENESIS_EGV70:
      ret.mass = 2230
      ret.wheelbase = 2.87
      ret.steerRatio = 14.6
    elif candidate == CAR.GENESIS_G80_RG3:
      ret.mass = 1785
      ret.wheelbase = 3.01
      ret.steerRatio = 16.5
    elif candidate == CAR.GENESIS_EG80_RG3:
      ret.mass = 2265.
      ret.wheelbase = 3.01
      ret.steerRatio = 16.5
    else:
      interface_community.get_params(candidate, ret)

    # *** longitudinal control ***
    if candidate in CANFD_CAR:
      ret.longitudinalTuning.kpV = [0.1]
      ret.longitudinalTuning.kiV = [0.0]
      ret.experimentalLongitudinalAvailable = (candidate in (HYBRID_CAR | EV_CAR) and candidate not in
                                               (CANFD_UNSUPPORTED_LONGITUDINAL_CAR | CANFD_RADAR_SCC_CAR))
    else:
      ret.longitudinalTuning.kpBP = [0., 5. * CV.KPH_TO_MS, 10. * CV.KPH_TO_MS, 30. * CV.KPH_TO_MS, 130. * CV.KPH_TO_MS]
      ret.longitudinalTuning.kpV = [1.2, 1.0, 0.95, 0.8, 0.25]
      ret.longitudinalTuning.kiBP = [0., 130. * CV.KPH_TO_MS]
      ret.longitudinalTuning.kiV = [0.1, 0.05]
      ret.stoppingDecelRate = 0.3

      ret.steerActuatorDelay = 0.1
      ret.steerLimitTimer = 2.0

      ret.experimentalLongitudinalAvailable = True #candidate not in (LEGACY_SAFETY_MODE_CAR)

    ret.openpilotLongitudinalControl = experimental_long and ret.experimentalLongitudinalAvailable
    ret.pcmCruise = not ret.openpilotLongitudinalControl

    ret.stoppingControl = True
    ret.startingState = True
    ret.vEgoStarting = 0.3
    ret.vEgoStopping = 0.3
    ret.startAccel = 1.0
    ret.longitudinalActuatorDelayLowerBound = 0.5
    ret.longitudinalActuatorDelayUpperBound = 0.5

    # *** feature detection ***
    if candidate in CANFD_CAR:
      ret.enableBsm = 0x1e5 in fingerprint[CAN.ECAN]
    else:
      ret.enableBsm = 0x58b in fingerprint[0]

    # *** panda safety config ***
    if candidate in CANFD_CAR:
      cfgs = [get_safety_config(car.CarParams.SafetyModel.hyundaiCanfd), ]
      if CAN.ECAN >= 4:
        cfgs.insert(0, get_safety_config(car.CarParams.SafetyModel.noOutput))
      ret.safetyConfigs = cfgs

      if ret.flags & HyundaiFlags.CANFD_HDA2:
        ret.safetyConfigs[-1].safetyParam |= Panda.FLAG_HYUNDAI_CANFD_HDA2
        if ret.flags & HyundaiFlags.CANFD_HDA2_ALT_STEERING:
          ret.safetyConfigs[-1].safetyParam |= Panda.FLAG_HYUNDAI_CANFD_HDA2_ALT_STEERING
      if ret.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
        ret.safetyConfigs[-1].safetyParam |= Panda.FLAG_HYUNDAI_CANFD_ALT_BUTTONS
      if ret.flags & HyundaiFlags.CANFD_CAMERA_SCC:
        ret.safetyConfigs[-1].safetyParam |= Panda.FLAG_HYUNDAI_CAMERA_SCC

      ret.sccBus = 0
    else:
      if candidate in LEGACY_SAFETY_MODE_CAR:
        # these cars require a special panda safety mode due to missing counters and checksums in the messages
        ret.safetyConfigs = [get_safety_config(car.CarParams.SafetyModel.hyundaiLegacy)]
      else:
        ret.safetyConfigs = [get_safety_config(car.CarParams.SafetyModel.hyundai, 0)]

      ret.sccBus = 2 if (candidate in CAMERA_SCC_CAR or Params().get_bool('SccOnBus2')) else 0
      ret.hasAutoHold = 1151 in fingerprint[0]
      ret.hasLfaHda = 1157 in fingerprint[0]
      ret.hasNav = 1348 in fingerprint[0]

      if not ret.openpilotLongitudinalControl:
        ret.radarUnavailable = ret.sccBus == -1

      if ret.sccBus == 2:
        ret.hasScc13 = 1290 in fingerprint[0] or 1290 in fingerprint[2]
        ret.hasScc14 = 905 in fingerprint[0] or 905 in fingerprint[2]
        ret.openpilotLongitudinalControl = True
        ret.radarUnavailable = False
        ret.safetyConfigs = [get_safety_config(car.CarParams.SafetyModel.hyundaiLegacy)]
        ret.radarTimeStep = 0.02  # 50hz

    if ret.openpilotLongitudinalControl and ret.sccBus == 0 and not Params().get_bool('CruiseStateControl'):
      ret.pcmCruise = False
    else:
      ret.pcmCruise = True # managed by cruise state manager

    if ret.openpilotLongitudinalControl:
      ret.safetyConfigs[-1].safetyParam |= Panda.FLAG_HYUNDAI_LONG
    if candidate in HYBRID_CAR:
      ret.safetyConfigs[-1].safetyParam |= Panda.FLAG_HYUNDAI_HYBRID_GAS
    elif candidate in EV_CAR:
      ret.safetyConfigs[-1].safetyParam |= Panda.FLAG_HYUNDAI_EV_GAS

    if candidate in (CAR.KONA, CAR.KONA_EV, CAR.KONA_HEV, CAR.KONA_EV_2022):
      ret.flags |= HyundaiFlags.ALT_LIMITS.value
      ret.safetyConfigs[-1].safetyParam |= Panda.FLAG_HYUNDAI_ALT_LIMITS

    if ret.centerToFront == 0:
      ret.centerToFront = ret.wheelbase * 0.4

    return ret

  @staticmethod
  def init(CP, logcan, sendcan):
    if is_radar_point(CP) and not (CP.flags & HyundaiFlags.CANFD_CAMERA_SCC.value):
      addr, bus = 0x7d0, 0
      if CP.flags & HyundaiFlags.CANFD_HDA2.value:
        addr, bus = 0x730, CanBus(CP).ECAN
      disable_ecu(logcan, sendcan, bus=bus, addr=addr, com_cont_req=b'\x28\x83\x01')

    # for blinkers
    if CP.flags & HyundaiFlags.ENABLE_BLINKERS:
      disable_ecu(logcan, sendcan, bus=CanBus(CP).ECAN, addr=0x7B1, com_cont_req=b'\x28\x83\x01')

  def _update(self, c):
    ret = self.CS.update(self.cp, self.cp_cam)

    if self.CS.cruise_buttons[-1] != self.CS.prev_cruise_buttons:
      ret.buttonEvents = create_button_events(self.CS.cruise_buttons[-1], self.CS.prev_cruise_buttons, BUTTONS_DICT)

    # On some newer model years, the CANCEL button acts as a pause/resume button based on the PCM state
    # To avoid re-engaging when openpilot cancels, check user engagement intention via buttons
    # Main button also can trigger an engagement on these cars
    allow_enable = any(btn in ENABLE_BUTTONS for btn in self.CS.cruise_buttons) or any(self.CS.main_buttons) \
                   or self.CP.carFingerprint in CANFD_CAR

    events = self.create_common_events(ret, pcm_enable=self.CS.CP.pcmCruise, allow_enable=allow_enable)

    # low speed steer alert hysteresis logic (only for cars with steer cut off above 10 m/s)
    if ret.vEgo < (self.CP.minSteerSpeed + 2.) and self.CP.minSteerSpeed > 10.:
      self.low_speed_alert = True
    if ret.vEgo > (self.CP.minSteerSpeed + 4.):
      self.low_speed_alert = False
    if self.low_speed_alert:
      events.add(car.CarEvent.EventName.belowSteerSpeed)

    ret.events = events.to_msg()

    return ret

  def apply(self, c, now_nanos):
    return self.CC.update(c, self.CS, now_nanos)

  @staticmethod
  def get_params_adjust_set_speed():
    return [8, 10], [12, 14, 16, 18]

  def create_buttons(self, button):

    if self.CP.carFingerprint in CANFD_CAR:
      return self.create_buttons_can_fd(button)
    else:
      return self.create_buttons_can(button)

  def get_buttons_dict(self):
    return BUTTONS_DICT

  def create_buttons_can(self, button):
    values = self.CS.clu11
    values["CF_Clu_CruiseSwState"] = button
    values["CF_Clu_AliveCnt1"] = (values["CF_Clu_AliveCnt1"] + 1) % 0x10
    return self.CC.packer.make_can_msg("CLU11", self.CP.sccBus, values)

  def create_buttons_can_fd(self, button):
    values = {
      "COUNTER": self.CS.buttons_counter+1,
      "SET_ME_1": 1,
      "CRUISE_BUTTONS": button,
    }
    return self.CC.packer.make_can_msg("CRUISE_BUTTONS", 5, values)

