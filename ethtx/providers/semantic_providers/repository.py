#  Copyright 2021 DAI Foundation
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at: http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from functools import lru_cache
from typing import Optional, List, Dict, Tuple

from ethtx.decoders.decoders.semantics import decode_events_and_functions
from ethtx.models.semantics_model import (
    AddressSemantics,
    ContractSemantics,
    ParameterSemantics,
    ERC20Semantics,
    TransformationSemantics,
    FunctionSemantics,
    EventSemantics,
    Signature,
    SignatureArg,
)
from ethtx.providers import EtherscanProvider, Web3Provider, ENSProvider
from ethtx.providers.semantic_providers.database import ISemanticsDatabase
from ethtx.semantics.protocols_router import amend_contract_semantics
from ethtx.semantics.solidity.precompiles import precompiles
from ethtx.semantics.standards.erc20 import ERC20_FUNCTIONS, ERC20_EVENTS
from ethtx.semantics.standards.erc721 import ERC721_FUNCTIONS, ERC721_EVENTS


class SemanticsRepository:
    def __init__(
        self,
        database_connection: ISemanticsDatabase,
        etherscan_provider: EtherscanProvider,
        web3provider: Web3Provider,
        ens_provider: ENSProvider,
    ):
        self.database = database_connection
        self.etherscan = etherscan_provider
        self._web3provider = web3provider
        self._ens_provider = ens_provider

        self._records: Optional[List] = None

    def record(self) -> None:
        """Records is an array used to hold semantics used in tx decing process.
        This recording is used just for logging"""
        self._records = []

    def end_record(self) -> List:
        tmp_records = self._records
        self._records = None
        return tmp_records

    def _read_stored_semantics(
        self, address: str, chain_id: str
    ) -> Optional[AddressSemantics]:
        def decode_parameter(_parameter):
            components_semantics = []
            if "components" in _parameter:
                for component in _parameter["components"]:
                    components_semantics.append(decode_parameter(component))

            decoded_parameter = ParameterSemantics(
                parameter_name=_parameter["parameter_name"],
                parameter_type=_parameter["parameter_type"],
                components=components_semantics,
                indexed=_parameter["indexed"],
                dynamic=_parameter["dynamic"],
            )

            return decoded_parameter

        if not address:
            return None

        ZERO_HASH = "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"

        raw_address_semantics = self.database.get_address_semantics(chain_id, address)

        if raw_address_semantics:

            if raw_address_semantics.get("erc20"):
                erc20_semantics = ERC20Semantics(
                    name=raw_address_semantics["erc20"]["name"],
                    symbol=raw_address_semantics["erc20"]["symbol"],
                    decimals=raw_address_semantics["erc20"]["decimals"],
                )
            else:
                erc20_semantics = None

            if raw_address_semantics["contract"] == ZERO_HASH:
                contract_semantics = ContractSemantics(
                    code_hash=raw_address_semantics["contract"], name="EOA"
                )

            else:

                raw_contract_semantics = self.database.get_contract_semantics(
                    raw_address_semantics["contract"]
                )
                events = {}

                for signature, event in raw_contract_semantics["events"].items():

                    parameters_semantics = []
                    for parameter in event["parameters"]:
                        parameters_semantics.append(decode_parameter(parameter))

                    events[signature] = EventSemantics(
                        signature=signature,
                        anonymous=event["anonymous"],
                        name=event["name"],
                        parameters=parameters_semantics,
                    )

                functions = {}
                for signature, function in raw_contract_semantics["functions"].items():

                    inputs_semantics = []
                    for parameter in function["inputs"]:
                        inputs_semantics.append(decode_parameter(parameter))
                    outputs_semantics = []
                    for parameter in function["outputs"]:
                        outputs_semantics.append(decode_parameter(parameter))

                    functions[signature] = FunctionSemantics(
                        signature=signature,
                        name=function["name"],
                        inputs=inputs_semantics,
                        outputs=outputs_semantics,
                    )

                transformations = {}
                for signature, parameters_transformations in raw_contract_semantics[
                    "transformations"
                ].items():
                    transformations[signature] = {}
                    for parameter, transformation in parameters_transformations.items():
                        transformations[signature][parameter] = TransformationSemantics(
                            transformed_name=transformation["transformed_name"],
                            transformed_type=transformation["transformed_type"],
                            transformation=transformation["transformation"],
                        )

                contract_semantics = ContractSemantics(
                    code_hash=raw_contract_semantics["code_hash"],
                    name=raw_contract_semantics["name"],
                    events=events,
                    functions=functions,
                    transformations=transformations,
                )

            name = raw_address_semantics.get("name", address)
            if name == address and not raw_address_semantics["is_contract"]:
                name = self._ens_provider.name(
                    provider=self._web3provider._get_node_connection(chain_id),
                    address=address,
                )

            address_semantics = AddressSemantics(
                chain_id=chain_id,
                address=address,
                name=name,
                is_contract=raw_address_semantics["is_contract"],
                contract=contract_semantics,
                standard=raw_address_semantics["standard"],
                erc20=erc20_semantics,
            )

            return address_semantics

        return None

    @lru_cache(maxsize=128)
    def get_semantics(self, chain_id: str, address: str) -> Optional[AddressSemantics]:

        if not address:
            return None

        ZERO_HASH = "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"

        address_semantics = self._read_stored_semantics(address, chain_id)
        if not address_semantics:

            # try to read the semantics form the Etherscan provider
            provider = self._web3provider
            code_hash = provider.get_code_hash(address, chain_id)

            if code_hash != ZERO_HASH:
                # smart contract
                raw_semantics, decoded = self.etherscan.contract.get_contract_abi(
                    chain_id, address
                )
                if decoded and raw_semantics:
                    # raw semantics received from Etherscan
                    events, functions = decode_events_and_functions(
                        raw_semantics["abi"]
                    )
                    standard, standard_semantics = self._decode_standard_semantics(
                        address, raw_semantics["name"], events, functions
                    )
                    if standard == "ERC20":
                        erc20_semantics = standard_semantics
                    else:
                        proxy_erc20 = provider.guess_erc20_proxy(address, chain_id)
                        if proxy_erc20:
                            erc20_semantics = ERC20Semantics(**proxy_erc20)
                        else:
                            erc20_semantics = None
                    contract_semantics = ContractSemantics(
                        code_hash=code_hash,
                        name=raw_semantics["name"],
                        events=events,
                        functions=functions,
                        transformations={},
                    )
                    address_semantics = AddressSemantics(
                        chain_id=chain_id,
                        address=address,
                        name=raw_semantics["name"],
                        is_contract=True,
                        contract=contract_semantics,
                        standard=standard,
                        erc20=erc20_semantics,
                    )

                else:
                    # try to guess if the address is a toke
                    potential_erc20_semantics = provider.guess_erc20_token(
                        address, chain_id
                    )
                    if potential_erc20_semantics:
                        standard = "ERC20"
                        erc20_semantics = ERC20Semantics(
                            name=potential_erc20_semantics["name"],
                            symbol=potential_erc20_semantics["symbol"],
                            decimals=potential_erc20_semantics["decimals"],
                        )
                    else:
                        standard = None
                        erc20_semantics = None

                    contract_semantics = ContractSemantics(
                        code_hash=code_hash, name=address
                    )
                    address_semantics = AddressSemantics(
                        chain_id=chain_id,
                        address=address,
                        name=address,
                        is_contract=True,
                        contract=contract_semantics,
                        standard=standard,
                        erc20=erc20_semantics,
                    )

            else:
                # externally owned address
                contract_semantics = ContractSemantics(code_hash=ZERO_HASH, name="EOA")
                name = self._ens_provider.name(
                    provider=self._web3provider._get_node_connection(chain_id),
                    address=address,
                )
                address_semantics = AddressSemantics(
                    chain_id=chain_id,
                    address=address,
                    name=name,
                    is_contract=False,
                    contract=contract_semantics,
                )

            self.update_semantics(address_semantics)

        # amend semantics with locally stored updates
        amend_contract_semantics(address_semantics.contract)

        if self._records is not None:
            self._records.append(address)

        return address_semantics

    def _decode_standard_semantics(
        self, address, name, events, functions
    ) -> Tuple[Optional[str], Optional[ERC20Semantics]]:
        standard = None
        standard_semantics = None

        if not address:
            return standard, standard_semantics

        if all(erc20_event in events for erc20_event in ERC20_EVENTS) and all(
            erc20_function in functions for erc20_function in ERC20_FUNCTIONS
        ):
            standard = "ERC20"
            try:
                provider = self._web3provider
                token_data = provider.get_erc20_token(address, name, functions)
                standard_semantics = ERC20Semantics(
                    name=token_data["name"],
                    symbol=token_data["symbol"],
                    decimals=token_data["decimals"],
                )
            except Exception:
                standard_semantics = ERC20Semantics(name=name, symbol=name, decimals=18)
        elif all(erc721_event in events for erc721_event in ERC721_EVENTS) and all(
            erc721_function in functions for erc721_function in ERC721_FUNCTIONS
        ):
            standard = "ERC721"
            standard_semantics = None

        return standard, standard_semantics

    @lru_cache(maxsize=128)
    def get_event_abi(self, chain_id, address, signature) -> Optional[EventSemantics]:

        if not address:
            return None

        semantics = self.get_semantics(chain_id, address)
        event_semantics = (
            semantics.contract.events.get(signature) if semantics else None
        )

        return event_semantics

    @lru_cache(maxsize=128)
    def get_transformations(
        self, chain_id, address, signature
    ) -> Optional[Dict[str, TransformationSemantics]]:

        if not address:
            return None

        semantics = self.get_semantics(chain_id, address)
        if semantics:
            transformations = semantics.contract.transformations.get(signature)
        else:
            transformations = None

        return transformations

    @lru_cache(maxsize=128)
    def get_anonymous_event_abi(self, chain_id, address) -> Optional[EventSemantics]:

        if not address:
            return None

        semantics = self.get_semantics(chain_id, address)
        event_semantics = None
        if semantics:
            anonymous_events = {
                signature
                for signature, event in semantics.contract.events.items()
                if event.anonymous
            }
            if len(anonymous_events) == 1:
                event_signature = anonymous_events.pop()
                event_semantics = semantics.contract.events[event_signature]

        return event_semantics

    @lru_cache(maxsize=128)
    def get_function_abi(
        self, chain_id, address, signature
    ) -> Optional[FunctionSemantics]:

        if not address:
            return None

        semantics = self.get_semantics(chain_id, address)
        function_semantics = (
            semantics.contract.functions.get(signature) if semantics else None
        )

        return function_semantics

    @lru_cache(maxsize=128)
    def get_constructor_abi(self, chain_id, address) -> Optional[FunctionSemantics]:

        if not address:
            return None

        semantics = self.get_semantics(chain_id, address)
        constructor_semantics = (
            semantics.contract.functions.get("constructor") if semantics else None
        )
        if constructor_semantics:
            constructor_semantics.outputs.append(
                ParameterSemantics(
                    parameter_name="__create_output__",
                    parameter_type="ignore",
                    indexed=False,
                    dynamic=True,
                )
            )

        return constructor_semantics

    def get_address_label(self, chain_id, address, proxies=None) -> str:

        if not address:
            return ""

        if int(address, 16) in precompiles:
            contract_label = "Precompiled"
        else:
            semantics = self.get_semantics(chain_id, address)
            if semantics.erc20:
                contract_label = semantics.erc20.symbol
            elif proxies and address in proxies:
                contract_label = proxies[address].name
            else:
                contract_label = (
                    semantics.name if semantics and semantics.name else address
                )

        return contract_label

    @lru_cache(maxsize=128)
    def check_is_contract(self, chain_id, address) -> bool:

        if not address:
            return False

        semantics = self.get_semantics(chain_id, address)
        is_contract = semantics is not None and semantics.is_contract

        return is_contract

    @lru_cache(maxsize=128)
    def get_standard(self, chain_id, address) -> Optional[str]:

        if not address:
            return None

        semantics = self.get_semantics(chain_id, address)
        standard = semantics.standard if semantics is not None else None

        return standard

    def get_token_data(
        self, chain_id, address, proxies=None
    ) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:

        if not address:
            return None, None, None, None

        semantics = self.get_semantics(chain_id, address)
        if semantics and semantics.erc20:
            token_name = (
                semantics.erc20.name if semantics and semantics.erc20 else address
            )
            token_symbol = (
                semantics.erc20.symbol if semantics and semantics.erc20 else "Unknown"
            )
            token_decimals = (
                semantics.erc20.decimals if semantics and semantics.erc20 else 18
            )
        elif proxies and address in proxies and proxies[address].token:
            token_name = proxies[address].token.name
            token_symbol = proxies[address].token.symbol
            token_decimals = proxies[address].token.decimals
        else:
            token_name = address
            token_symbol = "Unknown"
            token_decimals = 18

        return token_name, token_symbol, token_decimals, "ERC20"

    def update_address(self, chain_id, address, contract) -> Dict:

        updated_address = {"network": chain_id, "address": address, **contract}
        self.database.insert_address(address=updated_address, update_if_exist=True)

        return updated_address

    def update_semantics(self, semantics) -> None:

        if not semantics:
            return

        contract_id = self.database.insert_contract(
            contract=semantics.contract.dict(), update_if_exist=True
        )

        updated_address_semantics = semantics.copy()
        updated_address_semantics.contract = (
            updated_address_semantics.contract.code_hash
        )
        self.database.insert_address(
            address=updated_address_semantics.dict(), update_if_exist=True
        )

        if contract_id:
            self.insert_contract_signatures(semantics.contract)

    def insert_contract_signatures(self, contract_semantics: ContractSemantics) -> None:
        for _, v in contract_semantics.functions.items():

            if not v.signature.startswith("0x"):
                continue

            if v.inputs and v.inputs[0].parameter_type == "tuple":
                args = [
                    SignatureArg(name=param.parameter_name, type=param.parameter_type)
                    for param in v.inputs[0].components
                ]
            else:
                args = (
                    [
                        SignatureArg(
                            name=param.parameter_name, type=param.parameter_type
                        )
                        for param in v.inputs
                    ]
                    if v.inputs
                    else []
                )

            new_signature = Signature(
                signature_hash=v.signature, name=v.name, args=args
            )

            self.update_or_insert_signature(new_signature)

    def get_most_used_signature(self, signature_hash: str) -> Optional[Signature]:
        signatures = list(
            self.database.get_signature_semantics(signature_hash=signature_hash)
        )

        if signatures:
            most_common_signature = max(signatures, key=lambda x: x["count"])
            signature = Signature(
                signature_hash=most_common_signature["signature_hash"],
                name=most_common_signature["name"],
                args=most_common_signature["args"],
                count=most_common_signature["count"],
                tuple=most_common_signature["tuple"],
                guessed=most_common_signature["guessed"],
            )

            return signature

        return None

    def update_or_insert_signature(self, signature: Signature) -> None:
        signatures = self.database.get_signature_semantics(
            signature_hash=signature.signature_hash
        )
        for sig in signatures:
            if (
                signature.name == sig["name"]
                and signature.signature_hash == sig["signature_hash"]
                and len(signature.args) == len(sig["args"])
            ):
                if signature.args and any(
                    arg for arg in list(sig["args"][0].values()) if "arg" in arg
                ):
                    for index, argument in enumerate(sig["args"]):
                        argument["name"] = signature.args[index].name
                        argument["type"] = signature.args[index].type

                sig["count"] += 1
                sig["guessed"] = False
                self.database.insert_signature(signature=sig, update_if_exist=True)
                break

        else:
            self.database.insert_signature(signature=signature.dict())
