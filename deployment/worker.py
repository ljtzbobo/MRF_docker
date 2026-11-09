"""单 CID 容器进程：按命名端口接收数据包并维持组件状态。"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock

from components.implementations.runtime_registry import create_runtime_component
from components.specs import load_component_specs, load_protocol_specs
from matching.contracts import check_node_ports, validate_runtime_packet
from task_repr.template.refinement import SpecificationNode

from .protocol import decode_packet, encode_packet


MAX_REQUEST_BYTES = 24 * 1024 * 1024


def make_handler(component, node: SpecificationNode):
    protocols = load_protocol_specs()
    interface = load_component_specs(protocols=protocols)[component.cid]
    error = check_node_ports(node, component, interface, protocols, component.config)
    if error:
        raise ValueError(error)
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply(200, {"status": "ready", "cid": component.cid}) if self.path == "/health" else self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/process":
                self._reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise ValueError("request body size is invalid")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or set(request) != {"inputs"} or not isinstance(request["inputs"], dict):
                    raise ValueError("request requires named input ports")
                if set(request["inputs"]) != set(node.inputs):
                    raise ValueError("request does not cover all input ports")
                packets = {port: decode_packet(raw) for port, raw in request["inputs"].items()}
                input_ports = getattr(component, "input_ports", None) or {"in": component.input_port}
                for port, packet in packets.items():
                    error = validate_runtime_packet(node.inputs[port], packet, input_ports.get(port, component.input_port), protocols)
                    if error:
                        raise ValueError(error)
                with lock:
                    output = (component.process(next(iter(packets.values()))) if len(packets) == 1
                              else component.process_inputs(packets))
                format_name = next(iter(node.outputs.values()))
                error = validate_runtime_packet(format_name, output, component.output_port, protocols)
                if error:
                    raise ValueError(error)
                self._reply(200, {"output": encode_packet(output)})
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                self._reply(422, {"error": str(exc)})
            except Exception as exc:
                self._reply(500, {"error": str(exc)})

        def log_message(self, format, *args):
            return

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cid", required=True)
    parser.add_argument("--node-json", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    raw = json.loads(args.node_json)
    if set(raw) != {"id", "cid", "parameters", "inputs", "outputs"} or raw["cid"] != args.cid:
        raise ValueError("worker node configuration is invalid")
    component = create_runtime_component(args.cid, **raw["parameters"])
    node = SpecificationNode(raw["id"], raw["id"], "container_runtime",
                             raw["inputs"], raw["outputs"], {}, raw["id"])
    ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(component, node)).serve_forever()


if __name__ == "__main__":
    main()
