# SPDX-License-Identifier: MIT
# Copyright (c) 2022 MBition GmbH
import warnings
from copy import copy
from functools import cached_property
from itertools import chain
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, TypeVar, Union, cast
from xml.etree import ElementTree

from deprecation import deprecated

from .admindata import AdminData
from .audience import AdditionalAudience, Audience
from .communicationparameter import CommunicationParameterRef
from .companydata import CompanyData, create_company_datas_from_et
from .dataobjectproperty import DataObjectProperty, DtcDop
from .diagdatadictionaryspec import DiagDataDictionarySpec
from .diaglayerraw import DiagLayerRaw
from .diaglayertype import DiagLayerType
from .ecu_variant_patterns import EcuVariantPattern, create_ecu_variant_patterns_from_et
from .endofpdufield import EndOfPduField
from .envdata import EnvironmentData
from .envdatadesc import EnvironmentDataDescription
from .exceptions import DecodeError, OdxWarning
from .functionalclass import FunctionalClass
from .globals import logger
from .message import Message
from .multiplexer import Multiplexer
from .nameditemlist import NamedItemList
from .odxlink import OdxDocFragment, OdxLinkDatabase, OdxLinkId, OdxLinkRef
from .parentref import ParentRef
from .service import DiagService
from .singleecujob import SingleEcuJob
from .specialdata import SpecialDataGroup, create_sdgs_from_et
from .statechart import StateChart
from .structures import BasicStructure, Request, Response, create_any_structure_from_et
from .table import Table
from .units import UnitGroup, UnitSpec
from .utils import create_description_from_et, short_name_as_id

T = TypeVar("T")

PrefixTree = Dict[int, Union[List[DiagService], "PrefixTree"]]


class DiagLayer:
    """This class represents a "logical view" upon a diagnostic layer
    according to the ODX standard.

    i.e. it handles the value inheritance, communication parameters,
    encoding/decoding of data, etc.
    """

    def __init__(self, *, diag_layer_raw: DiagLayerRaw) -> None:
        self.diag_layer_raw = diag_layer_raw

    @staticmethod
    def from_et(et_element: ElementTree.Element, doc_frags: List[OdxDocFragment]) -> "DiagLayer":
        diag_layer_raw = DiagLayerRaw.from_et(et_element, doc_frags)

        # Create DiagLayer
        return DiagLayer(diag_layer_raw=diag_layer_raw)

    def _build_odxlinks(self) -> Dict[OdxLinkId, Any]:
        """Construct a mapping from IDs to all objects that are contained in this diagnostic layer."""
        result = self.diag_layer_raw._build_odxlinks()

        # we want to get the full diag layer, not just the raw layer
        # when referencing...
        result[self.odx_id] = self

        return result

    def _resolve_odxlinks(self, odxlinks: OdxLinkDatabase) -> None:
        """Recursively resolve all references."""

        self.diag_layer_raw._resolve_odxlinks(odxlinks)

    def _finalize_init(self, odxlinks: OdxLinkDatabase) -> None:
        """This method deals with everything inheritance related and
        -- after the final set of objects covered by the diagnostic
        layer is determined -- resolves any short name references in
        the diagnostic layer.

        TODO: In some corner cases, the short name resolution is not
        correct: E.g. Given three layers A, B, and C, where B and C
        derive from A and A defines the diagnostic communication
        SA. If now B overrides SA and there are short name references
        to SA in A, the object to which this reference is resolved is
        undefined. An easy fix for this problem is to copy all
        inherited objects in derived layers, but that would lead to
        excessive memory consumption for large databases...
        """

        #####
        # fill in all applicable objects that use value inheritance
        #####

        # diagnostic communication objects with the ODXLINKs resolved
        diag_comms = self._compute_available_diag_comms(odxlinks)
        self._diag_comms = NamedItemList(short_name_as_id, diag_comms)

        # filter the diag comms for services and single-ECU jobs
        services = [dc for dc in diag_comms if isinstance(dc, DiagService)]
        single_ecu_jobs = [dc for dc in diag_comms if isinstance(dc, SingleEcuJob)]
        self._services = NamedItemList(short_name_as_id, services)
        self._single_ecu_jobs = NamedItemList(short_name_as_id, single_ecu_jobs)

        global_negative_responses = self._compute_available_global_neg_responses(odxlinks)
        self._global_negative_responses = NamedItemList(short_name_as_id, global_negative_responses)

        tables = self._compute_available_tables()
        self._tables = NamedItemList(short_name_as_id, tables)

        functional_classes = self._compute_available_functional_classes()
        self._functional_classes = NamedItemList(short_name_as_id, functional_classes)

        additional_audiences = self._compute_available_additional_audiences()
        self._additional_audiences = NamedItemList(short_name_as_id, additional_audiences)

        state_charts = self._compute_available_state_charts()
        self._state_charts = NamedItemList(short_name_as_id, state_charts)

        ############
        # create a new unit_spec object. This is necessary because
        # unit_groups are subject to value inheritance.

        # unit groups applicable to this diaglayer (i.e., including
        # value inheritance)
        unit_groups = self._compute_available_unit_groups()

        # convenience variable for the locally-defined unit spec
        local_unit_spec: Optional[UnitSpec]
        if self.diag_layer_raw.diag_data_dictionary_spec is not None:
            local_unit_spec = self.diag_layer_raw.diag_data_dictionary_spec.unit_spec
        else:
            local_unit_spec = None

        unit_spec: Optional[UnitSpec]
        if local_unit_spec is None and not unit_groups:
            # no locally defined unit spec and no inherited unit groups
            unit_spec = None
        elif local_unit_spec is None:
            # no locally defined unit spec but inherited unit groups
            unit_spec = UnitSpec(
                unit_groups=NamedItemList(short_name_as_id, unit_groups),
                units=NamedItemList(short_name_as_id, []),
                physical_dimensions=NamedItemList(short_name_as_id, []),
                sdgs=[])
        else:
            # locally defined unit spec and inherited unit groups
            unit_spec = UnitSpec(
                unit_groups=NamedItemList(short_name_as_id, unit_groups),
                units=local_unit_spec.units,
                physical_dimensions=local_unit_spec.physical_dimensions,
                sdgs=[])
        ############

        dops = NamedItemList(short_name_as_id, self._compute_available_data_object_props())
        dtc_dops: NamedItemList[DtcDop]
        structures: NamedItemList[BasicStructure]
        end_of_pdu_fields: NamedItemList[EndOfPduField]
        env_data_descs: NamedItemList[EnvironmentDataDescription]
        env_datas: NamedItemList[EnvironmentData]
        muxs: NamedItemList[Multiplexer]
        ddds_sdgs: List[SpecialDataGroup]
        if self.diag_layer_raw.diag_data_dictionary_spec:
            dtc_dops = self.diag_layer_raw.diag_data_dictionary_spec.dtc_dops
            structures = self.diag_layer_raw.diag_data_dictionary_spec.structures
            end_of_pdu_fields = self.diag_layer_raw.diag_data_dictionary_spec.end_of_pdu_fields
            env_data_descs = self.diag_layer_raw.diag_data_dictionary_spec.env_data_descs
            env_datas = self.diag_layer_raw.diag_data_dictionary_spec.env_datas
            muxs = self.diag_layer_raw.diag_data_dictionary_spec.muxs
            ddds_sdgs = self.diag_layer_raw.diag_data_dictionary_spec.sdgs
        else:
            dtc_dops = NamedItemList(short_name_as_id)
            structures = NamedItemList(short_name_as_id)
            end_of_pdu_fields = NamedItemList(short_name_as_id)
            env_data_descs = NamedItemList(short_name_as_id)
            env_datas = NamedItemList(short_name_as_id)
            muxs = NamedItemList(short_name_as_id)
            ddds_sdgs = []

        # create a DiagDataDictionarySpec which includes all the
        # inherited objects. To me, this seems rather inelegant, but
        # hey, it's described like this in the standard.
        self._diag_data_dictionary_spec = \
            DiagDataDictionarySpec(
                data_object_props=dops,
                dtc_dops=dtc_dops,
                structures=structures,
                end_of_pdu_fields=end_of_pdu_fields,
                tables=NamedItemList(short_name_as_id, tables),
                env_data_descs=env_data_descs,
                env_datas=env_datas,
                muxs=muxs,
                unit_spec=unit_spec,
                sdgs=ddds_sdgs)

        #####
        # compute the communication parameters applicable to the
        # diagnostic layer. Note that communication parameters do
        # *not* use value inheritance, but a slightly different
        # scheme, cf the docstring of
        # _compute_available_commmunication_parameters().
        #####
        self._communication_parameters = self._compute_available_commmunication_parameters()

        #####
        # resolve all SNREFs. TODO: We allow SNREFS to objects that
        # were inherited by the diaglayer. This might not be allowed
        # by the spec (So far, I haven't found any definitive
        # statement...)
        #####
        self.diag_layer_raw._resolve_snrefs(self)

    #####
    # <properties forwarded to the "raw" diag layer>
    #####
    @property
    def variant_type(self) -> DiagLayerType:
        return self.diag_layer_raw.variant_type

    @property
    def odx_id(self) -> OdxLinkId:
        return self.diag_layer_raw.odx_id

    @property
    def short_name(self) -> str:
        return self.diag_layer_raw.short_name

    @property
    def long_name(self) -> Optional[str]:
        return self.diag_layer_raw.long_name

    @property
    def description(self) -> Optional[str]:
        return self.diag_layer_raw.description

    @property
    def admin_data(self) -> Optional[AdminData]:
        return self.diag_layer_raw.admin_data

    @property
    def company_datas(self) -> NamedItemList[CompanyData]:
        return self.diag_layer_raw.company_datas

    @property
    def requests(self) -> NamedItemList[Request]:
        return self.diag_layer_raw.requests

    @property
    def positive_responses(self) -> NamedItemList[Response]:
        return self.diag_layer_raw.positive_responses

    @property
    def negative_responses(self) -> NamedItemList[Response]:
        return self.diag_layer_raw.negative_responses

    @property
    def import_refs(self) -> List[OdxLinkRef]:
        return self.diag_layer_raw.import_refs

    @property
    def sdgs(self) -> List[SpecialDataGroup]:
        return self.diag_layer_raw.sdgs

    @property
    def parent_refs(self) -> List[ParentRef]:
        return self.diag_layer_raw.parent_refs

    @property
    def ecu_variant_patterns(self) -> List[EcuVariantPattern]:
        return self.diag_layer_raw.ecu_variant_patterns

    #####
    # </properties forwarded to the "raw" diag layer>
    #####

    #######
    # <stuff subject to value inheritance>
    #######
    @property
    def diag_comms(self) -> NamedItemList[Union[DiagService, SingleEcuJob]]:
        """All diagnostic communication primitives applicable to this DiagLayer

        Diagnostic communication primitives are diagnostic services as
        well as single-ECU jobs. This list has all references
        resolved.
        """
        return self._diag_comms

    @property
    def services(self) -> NamedItemList[DiagService]:
        """All diagnostic services applicable to this DiagLayer

        This is a subset of all diagnostic communication
        primitives. All references are resolved in the list returned.
        """
        return self._services

    @property
    def single_ecu_jobs(self) -> NamedItemList[SingleEcuJob]:
        """All single-ECU jobs applicable to this DiagLayer

        This is a subset of all diagnostic communication
        primitives. All references are resolved in the list returned.
        """
        return self._single_ecu_jobs

    @property
    def global_negative_responses(self) -> NamedItemList[Response]:
        """All global negative responses applicable to this DiagLayer"""
        return self._global_negative_responses

    @property
    def tables(self) -> NamedItemList[Table]:
        """All tables applicable to this DiagLayer"""
        return self._tables

    @property
    def functional_classes(self) -> NamedItemList[FunctionalClass]:
        """All functional classes applicable to this DiagLayer"""
        return self._functional_classes

    @property
    def state_charts(self) -> NamedItemList[StateChart]:
        """All state charts applicable to this DiagLayer"""
        return self._state_charts

    @property
    def additional_audiences(self) -> NamedItemList[AdditionalAudience]:
        """All audiences applicable to this DiagLayer"""
        return self._additional_audiences

    @property
    def diag_data_dictionary_spec(self) -> DiagDataDictionarySpec:
        """The DiagDataDictionarySpec applicable to this DiagLayer"""
        return self._diag_data_dictionary_spec

    #######
    # </stuff subject to value inheritance>
    #######

    #####
    # <value inheritance mechanism helpers>
    #####
    def _get_parent_refs_sorted_by_priority(self, reverse=False) -> Iterable[ParentRef]:
        return sorted(
            self.diag_layer_raw.parent_refs,
            key=lambda pr: pr.layer.variant_type.inheritance_priority,
            reverse=reverse)

    def _compute_available_objects(
        self,
        get_local_objects: Callable[["DiagLayer"], Iterable[T]],
        get_not_inherited: Callable[[ParentRef], Iterable[str]],
    ) -> Iterable[T]:
        """Helper method to compute the set of all objects applicable
        to the DiagLayer if these objects are subject to the value
        inheritance mechanism

        Note that all objects subject to the value inheritance
        mechanism exhibit a short_name attribute.

        :param get_local_objects: Function mapping a DiagLayer to the
        set of objects that are locally defined by that DiagLayer. If
        any of these objects have already been defined in any of the
        parents, the locally defined instance overrides them.

        :param get_not_inherited: Function mapping a ParentRef to the
        set of short names of the objects which shall not be inherited
        from the parents.

        """

        result_dict: Dict[str, T] = {}

        # populate the result dictionary with the inherited objects
        #
        # TODO (?): make sure that there are no "illegal" collisions
        # i.e., different objects with the same short name stemming
        # from parent layers exhibiting the same priority that are not
        # overwritten by a locally defined object. (IMO, this is quite
        # a corner case.)
        for parent_ref in self._get_parent_refs_sorted_by_priority():
            parent_dl = parent_ref.layer
            for dc in parent_dl._compute_available_objects(get_local_objects, get_not_inherited):
                result_dict[dc.short_name] = dc  # type: ignore[attr-defined]

            # remove the explictly not inherited objects
            for sn in get_not_inherited(parent_ref):
                if sn in result_dict:
                    del result_dict[sn]

        # consider the locally defined objects (override the
        # inherited entries or add new ones)
        for obj in get_local_objects(self):
            result_dict[obj.short_name] = obj  # type: ignore[attr-defined]

        return result_dict.values()

    def _get_local_diag_comms(self, odxlinks: OdxLinkDatabase
                             ) -> Iterable[Union[DiagService, SingleEcuJob]]:
        """Return the list of locally defined diagnostic communications.

        This is not completely trivial as it requires to resolving the
        references specified in the <DIAG-COMMS> XML tag.
        """
        result_dict: Dict[str, Union[DiagService, SingleEcuJob]] = {}

        # TODO (?): add objects from the import-refs

        for dc_proxy in self.diag_layer_raw.diag_comms:
            if isinstance(dc_proxy, OdxLinkRef):
                dc = odxlinks.resolve(dc_proxy)
            else:
                dc = dc_proxy

            assert isinstance(dc, (DiagService, SingleEcuJob))
            assert dc.short_name not in result_dict, (
                f"Multiple definitions of DIAG-COMM '{dc.short_name}' in "
                f"layer '{self.short_name}'")
            result_dict[dc.short_name] = dc

        return result_dict.values()

    def _get_local_unit_groups(self) -> Iterable[UnitGroup]:
        if self.diag_layer_raw.diag_data_dictionary_spec is None:
            return []

        unit_spec = self.diag_layer_raw.diag_data_dictionary_spec.unit_spec
        if unit_spec is None:
            return []

        return unit_spec.unit_groups

    def _compute_available_diag_comms(self, odxlinks: OdxLinkDatabase
                                     ) -> Iterable[Union[DiagService, SingleEcuJob]]:
        get_local_objects_fn = lambda dl: dl._get_local_diag_comms(odxlinks)
        not_inherited_fn = lambda parent_ref: parent_ref.not_inherited_diag_comms
        return self._compute_available_objects(get_local_objects_fn, not_inherited_fn)

    def _compute_available_global_neg_responses(self, odxlinks: OdxLinkDatabase) \
            -> Iterable[Response]:
        get_local_objects_fn = lambda dl: dl.diag_layer_raw.global_negative_responses
        not_inherited_fn = lambda parent_ref: parent_ref.not_inherited_global_neg_responses
        return self._compute_available_objects(get_local_objects_fn, not_inherited_fn)

    def _compute_available_data_object_props(self) -> Iterable[DataObjectProperty]:
        get_local_objects_fn = lambda dl: \
            [] if dl.diag_layer_raw.diag_data_dictionary_spec is None \
            else dl.diag_layer_raw.diag_data_dictionary_spec.data_object_props
        not_inherited_fn = lambda parent_ref: parent_ref.not_inherited_dops
        return self._compute_available_objects(get_local_objects_fn, not_inherited_fn)

    def _compute_available_tables(self) -> Iterable[Table]:
        get_local_objects_fn = lambda dl: \
            [] if dl.diag_layer_raw.diag_data_dictionary_spec is None \
            else dl.diag_layer_raw.diag_data_dictionary_spec.tables
        not_inherited_fn = lambda parent_ref: parent_ref.not_inherited_tables
        return self._compute_available_objects(get_local_objects_fn, not_inherited_fn)

    def _compute_available_functional_classes(self) -> Iterable[FunctionalClass]:
        get_local_objects_fn = lambda dl: dl.diag_layer_raw.functional_classes
        not_inherited_fn: Callable[[ParentRef], List[str]] = lambda parent_ref: []
        return self._compute_available_objects(get_local_objects_fn, not_inherited_fn)

    def _compute_available_additional_audiences(self) -> Iterable[AdditionalAudience]:
        get_local_objects_fn = lambda dl: dl.diag_layer_raw.additional_audiences
        not_inherited_fn: Callable[[ParentRef], List[str]] = lambda parent_ref: []
        return self._compute_available_objects(get_local_objects_fn, not_inherited_fn)

    def _compute_available_state_charts(self) -> Iterable[StateChart]:
        get_local_objects_fn = lambda dl: dl.diag_layer_raw.state_charts
        not_inherited_fn: Callable[[ParentRef], List[str]] = lambda parent_ref: []
        return self._compute_available_objects(get_local_objects_fn, not_inherited_fn)

    def _compute_available_unit_groups(self) -> Iterable[UnitGroup]:
        get_local_objects_fn = lambda dl: dl._get_local_unit_groups()
        not_inherited_fn: Callable[[ParentRef], List[str]] = lambda parent_ref: []
        return self._compute_available_objects(get_local_objects_fn, not_inherited_fn)

    #####
    # </value inheritance mechanism helpers>
    #####

    #####
    # <communication parameter handling>
    #####
    def _compute_available_commmunication_parameters(self) -> List[CommunicationParameterRef]:
        """Compute the list of communication parameters that apply to
        the diagnostic layer

        Be aware that the inheritance scheme for communication
        parameters is slightly different than for objects that are
        subject to value inheritance:

        - The ODXLINK ID id of communication parameters is used to
          override inherited parameters instead of the short name.
        - A parameter is only overridden if the specified protocol
          matches.

        Note that the specification leaves some room for
        interpretation here: It says that if no protocol is specified,
        the parameter shall apply to any protocol. But what happens if
        the the same comparam is specified with and without a
        protocol? Is this allowed at all? If yes, which of these
        definitions gets priority? How does this interact with
        inheritance? The approach taken here is to allow such cases
        and to use the specific comparams if possible whilst the ones
        without a specified protocol are taken as fallbacks...

        """
        com_params_dict: Dict[Tuple[str, Optional[str]], CommunicationParameterRef] = dict()

        # Look in parent refs for inherited communication
        # parameters. First fetch the communication parameters from
        # low priority parents, then update with increasing priority.
        for parent_ref in self._get_parent_refs_sorted_by_priority():
            for cp in parent_ref.layer._compute_available_commmunication_parameters():
                com_params_dict[(cp.id_ref.ref_id, cp.protocol_snref)] = cp

        # finally, handle the locally defined communication parameters
        for cp in self.diag_layer_raw.communication_parameters:
            com_params_dict[(cp.id_ref.ref_id, cp.protocol_snref)] = cp

        return list(com_params_dict.values())

    @property
    def communication_parameters(self) -> List[CommunicationParameterRef]:
        """All communication parameters applicable to this DiagLayer

        Note that, although communication parameters use inheritance,
        it is *not* the "value inheritance" scheme used by e.g. DOPs,
        tables, state charts, ...
        """
        return self._communication_parameters

    @property
    def protocols(self) -> NamedItemList["DiagLayer"]:
        """Return the set of all protocols which are applicable to the diagnostic layer

        Note that protocols are *not* explicitly inherited objects,
        but the parent diagnostic layers of variant type "PROTOCOL".
        """
        result_dict: Dict[str, DiagLayer] = dict()

        for parent_ref in self._get_parent_refs_sorted_by_priority():
            for prot in parent_ref.layer.protocols:
                result_dict[prot.short_name] = prot

        if self.diag_layer_raw.variant_type == DiagLayerType.PROTOCOL:
            result_dict[self.diag_layer_raw.short_name] = self

        return NamedItemList(short_name_as_id, result_dict.values())

    def get_communication_parameter(
        self,
        cp_short_name: str,
        *,
        is_functional: Optional[bool] = None,
        protocol_name: Optional[str] = None,
    ) -> Optional[CommunicationParameterRef]:
        """Find a specific communication parameter according to some criteria.

        Setting a given parameter to `None` means "don't care"."""

        # determine the set of applicable communication parameters
        cps = [cp for cp in self.communication_parameters if cp.short_name == cp_short_name]
        if is_functional is not None:
            cps = [cp for cp in cps if cp.is_functional in (None, is_functional)]
        if protocol_name is not None:
            cps = [cp for cp in cps if cp.protocol_snref in (None, protocol_name)]

        if len(cps) > 1:
            warnings.warn(
                f"Communication parameter `{cp_short_name}` specified more "
                f"than once. Using first occurence.",
                OdxWarning,
            )
        elif len(cps) == 0:
            return None

        return cps[0]

    def get_can_receive_id(self, protocol_name: Optional[str] = None) -> Optional[int]:
        """CAN ID to which the ECU listens for diagnostic messages"""
        com_param = self.get_communication_parameter(
            "CP_UniqueRespIdTable", protocol_name=protocol_name)
        if com_param is None:
            return None

        with warnings.catch_warnings():
            # depending on the protocol, we may get
            # "Communication parameter 'CP_UniqueRespIdTable' does not
            # specify 'CP_CanPhysReqId'" warning here. we don't want this
            # warning and simply return None...
            warnings.simplefilter("ignore", category=OdxWarning)
            result = com_param.get_subvalue("CP_CanPhysReqId")
        if not result:
            return None
        assert isinstance(result, str)

        return int(result)

    @deprecated(details="use get_can_receive_id()")
    def get_receive_id(self) -> Optional[int]:
        return self.get_can_receive_id()

    def get_can_send_id(self, protocol_name: Optional[str] = None) -> Optional[int]:
        """CAN ID to which the ECU sends replies to diagnostic messages"""

        # this hopefully resolves to the 'CP_UniqueRespIdTable'
        # parameter from the ISO_15765_2 comparam subset. (There is a
        # parameter with the same name in the ISO_13400_2_DIS_2015
        # subset for DoIP. If the wrong one is retrieved, try
        # specifying the protocol_name.)
        com_param = self.get_communication_parameter(
            "CP_UniqueRespIdTable", protocol_name=protocol_name)
        if com_param is None:
            return None

        with warnings.catch_warnings():
            # depending on the protocol, we may get
            # "Communication parameter 'CP_UniqueRespIdTable' does not
            # specify 'CP_CanRespUSDTId'" warning here. we don't want this
            # warning and simply return None...
            warnings.simplefilter("ignore", category=OdxWarning)
            result = com_param.get_subvalue("CP_CanRespUSDTId")
        if not result:
            return None
        assert isinstance(result, str)

        return int(result)

    @deprecated(details="use get_can_send_id()")
    def get_send_id(self) -> Optional[int]:
        return self.get_can_send_id()

    def get_can_func_req_id(self, protocol_name: Optional[str] = None) -> Optional[int]:
        """CAN Functional Request Id."""
        com_param = self.get_communication_parameter("CP_CanFuncReqId", protocol_name=protocol_name)
        if com_param is None:
            return None

        result = com_param.get_value()
        if not result:
            return None
        assert isinstance(result, str)

        return int(result)

    def get_doip_logical_ecu_address(self, protocol_name: Optional[str] = None) -> Optional[int]:
        """Return the address of the ECU when using functional addressing.

        The parameter protocol_name is used to distinguish between
        different interfaces, e.g., offboard and onboard DoIP
        Ethernet.
        """

        # this hopefully resolves to the 'CP_UniqueRespIdTable'
        # parameter from the ISO_13400_2_DIS_2015 comparam
        # subset. (There is a parameter with the same name in the
        # ISO_15765_2 subset for CAN. If the wrong one is retrieved,
        # try specifying the protocol_name.)
        com_param = self.get_communication_parameter(
            "CP_UniqueRespIdTable", protocol_name=protocol_name, is_functional=False)

        if com_param is None:
            return None

        # The CP_DoIPLogicalEcuAddress is specified by the
        # "CP_DoIPLogicalEcuAddress" subvalue of the complex Comparam
        # CP_UniqueRespIdTable of the ISO_13400_2_DIS_2015 comparam
        # subset. Depending of the underlying transport protocol,
        # (i.e., CAN using ISO-TP) this subvalue might not exist.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=OdxWarning)
            ecu_addr = com_param.get_subvalue("CP_DoIPLogicalEcuAddress")
        if ecu_addr is None:
            return None
        return int(ecu_addr)

    def get_doip_logical_gateway_address(self,
                                         protocol_name: Optional[str] = None,
                                         is_functional: Optional[bool] = False) -> Optional[int]:
        """The logical gateway address for the diagnosis over IP transport protocol"""

        # retrieve CP_DoIPLogicalGatewayAddress from the
        # ISO_13400_2_DIS_2015 subset. hopefully.
        com_param = self.get_communication_parameter(
            "CP_DoIPLogicalGatewayAddress",
            is_functional=is_functional,
            protocol_name=protocol_name)
        if com_param is None:
            return None

        result = com_param.get_value()
        if not result:
            return None
        assert isinstance(result, str)

        return int(result)

    def get_doip_logical_tester_address(self,
                                        protocol_name: Optional[str] = None,
                                        is_functional: Optional[bool] = False) -> Optional[int]:
        """DoIp logical gateway address"""

        # retrieve CP_DoIPLogicalTesterAddress from the
        # ISO_13400_2_DIS_2015 subset. hopefully.
        com_param = self.get_communication_parameter(
            "CP_DoIPLogicalTesterAddress", is_functional=is_functional, protocol_name=protocol_name)
        if com_param is None:
            return None

        result = com_param.get_value()
        if not result:
            return None
        assert isinstance(result, str)

        return int(result)

    def get_doip_logical_functional_address(self,
                                            protocol_name: Optional[str] = None,
                                            is_functional: Optional[bool] = False) -> Optional[int]:
        """The logical functional DoIP address of the ECU."""

        # retrieve CP_DoIPLogicalFunctionalAddress from the
        # ISO_13400_2_DIS_2015 subset. hopefully.
        com_param = self.get_communication_parameter(
            "CP_DoIPLogicalFunctionalAddress",
            is_functional=is_functional,
            protocol_name=protocol_name,
        )
        if com_param is None:
            return None

        result = com_param.get_value()
        if not result:
            return None
        assert isinstance(result, str)

        return int(result)

    def get_doip_routing_activation_timeout(self,
                                            protocol_name: Optional[str] = None) -> Optional[float]:
        """The timout for the DoIP routing activation request in seconds"""

        # retrieve CP_DoIPRoutingActivationTimeout from the
        # ISO_13400_2_DIS_2015 subset. hopefully.
        com_param = self.get_communication_parameter(
            "CP_DoIPRoutingActivationTimeout", protocol_name=protocol_name)
        if com_param is None:
            return None

        result = com_param.get_value()
        if not result:
            return None
        assert isinstance(result, str)

        return float(result) / 1e6

    def get_doip_routing_activation_type(self,
                                         protocol_name: Optional[str] = None) -> Optional[int]:
        """The DoIP routing activation type

        The number returned has the following meaning:

        0          Default
        1          WWH-OBD (worldwide harmonized onboard diagnostic).
        2-0xDF     reserved
        0xE0-0xFF  OEM-specific
        """

        # retrieve CP_DoIPRoutingActivationType from the
        # ISO_13400_2_DIS_2015 subset. hopefully.
        com_param = self.get_communication_parameter(
            "CP_DoIPRoutingActivationType", protocol_name=protocol_name)
        if com_param is None:
            return None

        result = com_param.get_value()
        if not result:
            return None
        assert isinstance(result, str)

        return int(result)

    def get_tester_present_time(self, protocol_name: Optional[str] = None) -> Optional[float]:
        """Timeout on inactivity in seconds.

        This is defined by the communication parameter "CP_TesterPresentTime".

        Description of the comparam: "Time between a response and the
        next subsequent tester present message (if no other request is
        sent to this ECU) in case of physically addressed requests."
        """

        # retrieve CP_TesterPresentTime from either the
        # ISO_13400_2_DIS_2015 or the ISO_15765_2 subset.
        com_param = self.get_communication_parameter(
            "CP_TesterPresentTime", protocol_name=protocol_name)
        if com_param is None:
            return None

        result = com_param.get_value()
        if not result:
            return None
        assert isinstance(result, str)

        # the comparam specifies microseconds. convert this to seconds
        return float(result) / 1e6

    #####
    # </communication parameter handling>
    #####

    #####
    # <PDU decoding>
    #####

    @cached_property
    def _prefix_tree(self) -> PrefixTree:
        """Constructs the coded prefix tree of the services.

        Each leaf node is a list of `DiagService`s.  (This is because
        navigating from a service to the request/ responses is easier
        than finding the service for a given request/response object.)

        Example:
        Let there be four services with corresponding requests:
        * Request 1 has the coded constant prefix `12 34`.
        * Request 2 has the coded constant prefix `12 34`.
        * Request 3 has the coded constant prefix `12 56`.
        * Request 4 has the coded constant prefix `12 56 00`.

        Then, the constructed prefix tree is the dict
        ```
        {0x12: {0x34: {-1: [<Service 1>, <Service 2>]},
                0x56: {-1: [<Service 3>],
                       0x0: {-1: [<Service 4>]}
                       }}}
        ```
        Note, that the inner `-1` are constant to distinguish them
        from possible service IDs.

        Also note, that it is actually allowed that
        (a) SIDs for different services are the same like for service 1 and 2 (thus each leaf node is a list) and
        (b) one SID is the prefix of another SID like for service 3 and 4 (thus the constant `-1` key).

        """
        prefix_tree: PrefixTree = {}
        for s in self.services:
            # Compute prefixes for the service's request and all
            # possible responses. We need to consider the global
            # negative responses here, because they might contain
            # MATCHING-REQUEST parameters. If these global responses
            # do not contain such parameters, this will potentially
            # result in an enormous amount of decoded messages for
            # global negative responses. (I.e., one for each
            # service. This can be avoided by specifying the
            # corresponding request for `decode_response()`.)
            request_prefix = s.request.coded_const_prefix()
            prefixes = [request_prefix]
            prefixes += [
                x.coded_const_prefix(request_prefix=request_prefix) for x in chain(
                    s.positive_responses, s.negative_responses, self.global_negative_responses)
            ]
            for coded_prefix in prefixes:
                self._extend_prefix_tree(prefix_tree, coded_prefix, s)

        return prefix_tree

    @staticmethod
    def _extend_prefix_tree(prefix_tree: PrefixTree, coded_prefix: bytes,
                            service: DiagService) -> None:

        # make sure that tree has an entry for the given prefix
        sub_tree = prefix_tree
        for b in coded_prefix:
            if b not in sub_tree:
                sub_tree[b] = {}
            sub_tree = cast(PrefixTree, sub_tree[b])

        # Store the object as in the prefix tree. This is done by
        # assigning the list of possible objects to the key -1 of the
        # dictionary (this is quite hacky...)
        if sub_tree.get(-1) is None:
            sub_tree[-1] = [service]
        else:
            cast(List[DiagService], sub_tree[-1]).append(service)

    def _find_services_for_uds(self, message: Union[bytes, bytearray]) -> List[DiagService]:
        prefix_tree = self._prefix_tree

        # Find matching service(s) in prefix tree
        possible_services: List[DiagService] = []
        for b in message:
            if b in prefix_tree:
                assert isinstance(prefix_tree[b], dict)
                prefix_tree = cast(PrefixTree, prefix_tree[b])
            else:
                break
            if -1 in prefix_tree:
                possible_services += cast(List[DiagService], prefix_tree[-1])
        return possible_services

    def _decode(self, message: Union[bytes, bytearray],
                candidate_services: Iterable[DiagService]) -> List[Message]:
        decoded_messages: List[Message] = []

        for service in candidate_services:
            try:
                decoded_messages.append(service.decode_message(message))
            except DecodeError as e:
                # check if the message can be decoded as a global
                # negative response for the service
                for gnr in self.global_negative_responses:
                    try:
                        decoded_gnr = gnr.decode(message)
                        decoded_messages.append(
                            Message(
                                coded_message=message,
                                service=service,
                                structure=gnr,
                                param_dict=decoded_gnr))
                    except DecodeError:
                        pass

        if len(decoded_messages) == 0:
            raise DecodeError(
                f"None of the services {candidate_services} could parse {message.hex()}.")

        return decoded_messages

    def decode(self, message: Union[bytes, bytearray]) -> List[Message]:
        candidate_services = self._find_services_for_uds(message)

        return self._decode(message, candidate_services)

    def decode_response(self, response: Union[bytes, bytearray],
                        request: Union[bytes, bytearray, Message]) -> Iterable[Message]:
        if isinstance(request, Message):
            candidate_services = [request.service]
        else:
            if not isinstance(request, (bytes, bytearray)):
                raise TypeError(f"Request parameter must have type "
                                f"Message, bytes or bytearray but was {type(request)}")
            candidate_services = self._find_services_for_uds(request)
        if candidate_services is None:
            raise DecodeError(f"Couldn't find corresponding service for request {request.hex()}.")

        return self._decode(response, candidate_services)

    #####
    # </PDU decoding>
    #####

    def __str__(self) -> str:
        return f"DiagLayer('{self.short_name}', type='{self.variant_type.value}')"
