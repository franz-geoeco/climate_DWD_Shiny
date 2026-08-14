def classFactory(iface):
    from .dwd_climate_explorer import DWDClimateExplorerPlugin
    return DWDClimateExplorerPlugin(iface)
