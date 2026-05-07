"""
CSI Identity service — plugin info and capabilities.
"""

from generated import csi_pb2, csi_pb2_grpc

DRIVER_NAME = "luks.csi.example.com"
DRIVER_VERSION = "0.1.0"


class IdentityServicer(csi_pb2_grpc.IdentityServicer):
    def GetPluginInfo(self, request, context):
        return csi_pb2.GetPluginInfoResponse(
            name=DRIVER_NAME,
            vendor_version=DRIVER_VERSION,
        )

    def GetPluginCapabilities(self, request, context):
        # Advertise that this driver has a Controller service that can
        # create/delete volumes.
        return csi_pb2.GetPluginCapabilitiesResponse(
            capabilities=[
                csi_pb2.PluginCapability(
                    service=csi_pb2.PluginCapability.Service(
                        type=csi_pb2.PluginCapability.Service.CONTROLLER_SERVICE,
                    )
                ),
            ]
        )

    def Probe(self, request, context):
        return csi_pb2.ProbeResponse()
