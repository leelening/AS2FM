# Copyright (c) 2024 - for information on the respective copyright owner
# see the NOTICE file

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Functions for the conversion from SCXML to Jani.

The main entrypoint is `convert_scxml_root_to_jani_automaton`.
"""

from typing import Any, Dict, List, Optional

from as2fm.as2fm_common.logging import log_error
from as2fm.jani_generator.jani_entries import (
    JaniAssignment,
    JaniAutomaton,
    JaniExpression,
    JaniExpressionType,
    JaniModel,
    JaniVariable,
)
from as2fm.jani_generator.jani_entries.jani_expression_generator import (
    and_operator,
    array_access_operator,
    equal_operator,
)
from as2fm.jani_generator.jani_entries.jani_helpers import expand_random_variables_in_jani_model
from as2fm.jani_generator.jani_entries.jani_utils import is_expression_array, is_variable_array
from as2fm.jani_generator.ros_helpers.ros_communication_handler import (
    remove_empty_self_loops_from_interface_handlers_in_jani,
)
from as2fm.jani_generator.scxml_helpers.scxml_event import EventsHolder
from as2fm.jani_generator.scxml_helpers.scxml_event_processor import (
    implement_scxml_events_as_jani_syncs,
)
from as2fm.jani_generator.scxml_helpers.scxml_expression import get_array_length_var_name
from as2fm.jani_generator.scxml_helpers.scxml_to_jani_interfaces import BaseTag
from as2fm.scxml_converter.scxml_entries import ScxmlRoot


def convert_scxml_root_to_jani_automaton(
    scxml_root: ScxmlRoot,
    events_holder: EventsHolder,
    max_array_size: int,
) -> JaniAutomaton:
    """
    Convert an SCXML element to a Jani automaton.

    :param element: The SCXML element to convert (Must be the root scxml tag of the file).
    :param jani_automaton: The Jani automaton to write the converted element to.
    :param events_holder: The holder for the events to be implemented as Jani syncs.
    :param max_array_size: The max size of the arrays in the model.
    """
    jani_automaton = JaniAutomaton()
    BaseTag.from_element(
        scxml_root, [], (jani_automaton, events_holder), max_array_size
    ).write_model()
    return jani_automaton


def convert_multiple_scxmls_to_jani(scxmls: List[ScxmlRoot], max_array_size: int) -> JaniModel:
    """
    Assemble automata from multiple SCXML files into a Jani model.

    :param scxmls: List of SCXML Root objects (or file paths) to be included in the Jani model.
    :param timers: List of ROS timers to be included in the Jani model.
    :param max_time_ns: The maximum time in nanoseconds.
    :param max_array_size: The max size of the arrays in the model.
    :return: The Jani model containing the converted automata.
    """
    base_model = JaniModel()
    base_model.add_feature("arrays")
    base_model.add_feature("trigonometric-functions")
    events_holder = EventsHolder()
    for input_scxml in scxmls:
        assert isinstance(input_scxml, ScxmlRoot)
        assert (
            input_scxml.is_plain_scxml()
        ), f"Input model {input_scxml.get_name()} does not contain a plain SCXML model."
        try:
            automaton = convert_scxml_root_to_jani_automaton(
                input_scxml, events_holder, max_array_size
            )
        except Exception as e:
            log_error(
                input_scxml.get_xml_origin(),
                f"Error while converting SCXML model {input_scxml.get_name()}.",
            )
            raise e
        base_model.add_jani_automaton(automaton)
    implement_scxml_events_as_jani_syncs(events_holder, max_array_size, base_model)
    remove_empty_self_loops_from_interface_handlers_in_jani(base_model)
    expand_random_variables_in_jani_model(base_model, n_options=100)
    return base_model


def preprocess_jani_expressions(jani_model: JaniModel):
    """
    Preprocess JANI expressions in the model to be compatible with the standard JANI format.

    In the current state, this ensures that array comparison is expanded to evaluate each element.
    """
    global_variables = jani_model.get_variables()
    for jani_automaton in jani_model.get_automata():
        context_variables = global_variables | jani_automaton.get_variables()
        for jani_edge in jani_automaton.get_edges():
            if jani_edge.guard is not None:
                guard_exp = jani_edge.guard.get_expression()
                if guard_exp is not None:
                    jani_edge.guard.set_expression(
                        _preprocess_jani_expression(guard_exp, context_variables)
                    )
            for jani_destination in jani_edge.destinations:
                assignments: List[JaniAssignment] = jani_destination["assignments"]
                for assignment in assignments:
                    assignment.set_expression(
                        _preprocess_jani_expression(assignment.get_expression(), context_variables)
                    )
    for property in jani_model.get_properties():
        property_operands = property.get_property_operands()
        for property_exp in property_operands.values():
            property_exp.reset(_preprocess_jani_expression(property_exp, global_variables))


def _preprocess_jani_expression(
    jani_expression: JaniExpression, context_vars: Dict[str, JaniVariable]
) -> JaniExpression:
    exp_operator, exp_operands = jani_expression.as_operator()
    if exp_operator is None or is_expression_array(jani_expression):
        # Skip elements that are jani values, variable names or array values.
        return jani_expression
    assert exp_operands is not None
    has_array_operator = any(is_expression_array(exp_op) for exp_op in exp_operands.values())
    if has_array_operator:
        assert (
            exp_operator == "="
        ), "Array operators can be only used for assignments and comparisons."
        return _preprocess_array_comparison(jani_expression, context_vars)
    new_expr_dict: Dict[str, Any] = {"op": exp_operator}
    for operand_name, operand_expr in exp_operands.items():
        new_expr_dict.update(
            {operand_name: _preprocess_jani_expression(operand_expr, context_vars)}
        )
    new_expr = JaniExpression(new_expr_dict)
    new_expr.comment = jani_expression.comment
    return new_expr


def _preprocess_array_comparison(
    jani_expression: JaniExpression, context_vars: Dict[str, JaniVariable]
) -> JaniExpression:
    """Preprocess comparison between a constant array and a variable."""
    exp_operator, exp_operands = jani_expression.as_operator()
    assert exp_operator == "=", f"Expected an '=' operator, found {exp_operator}."
    assert exp_operands is not None
    array_elements: Optional[List[JaniExpression]] = None
    array_var_id = None
    array_length_var_ids = []
    for operand in exp_operands.values():
        expr_type = operand.get_expression_type()
        if expr_type == JaniExpressionType.IDENTIFIER:
            array_var_id = operand.as_identifier()
            assert isinstance(array_var_id, str)
            array_variable = context_vars.get(array_var_id)
            assert (
                array_variable is not None
            ), f"Can't find '{array_var_id}' in context vars {context_vars.keys()}."
            assert is_variable_array(array_variable), f"Variable {array_var_id} is not an array."
            array_info = array_variable.get_array_info()
            assert array_info is not None, f"Cannot get array_info from JANI Var. {array_var_id}."
            array_length_var_ids = [
                get_array_length_var_name(array_var_id, d + 1)
                for d in range(array_info.array_dimensions)
            ]
            assert all(
                array_len_id in context_vars for array_len_id in array_length_var_ids
            ), f"Missing length variables in context. Checked vars: {array_length_var_ids}."
        else:
            array_operator, array_operands = operand.as_operator()
            assert array_operator == "av", f"Expected {operand.as_dict()} has op=='av'."
            assert isinstance(array_operands, dict), "Expect array_operands to be a dict."
            assert isinstance(array_operands["elements"], list), "Invalid 'av' operator's content."
            array_elements = array_operands["elements"]
            assert all(
                array_entry.get_expression_type() == JaniExpressionType.LITERAL
                for array_entry in array_elements
            ), "Found non-literal expressions in 'av' operator. Are all array comparisons in 1D?"
    assert array_operator is not None, "No array operator found in the eq. operator."
    assert array_var_id is not None, "No array variable found in the eq. operator."
    assert isinstance(array_elements, list), f"Unexpected value for array elements {array_elements}"
    # Turn equality into a series of equality checks (exp. length and entry values)
    n_elements = len(array_elements)
    assert (
        len(array_length_var_ids) == 1
    ), "Trying to make comparisons across multi-dimensional arrays. Unsupported."
    last_expr = equal_operator(array_length_var_ids[0], n_elements)
    for idx in range(n_elements):
        last_expr = and_operator(
            last_expr, equal_operator(array_elements[idx], array_access_operator(array_var_id, idx))
        )
    # Preserve the comment in the JANI file
    last_expr.comment = jani_expression.comment
    return last_expr
