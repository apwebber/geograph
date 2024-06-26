"""This module contains the GeoGraphViewer to visualise GeoGraphs"""
from __future__ import annotations
import logging
import threading
import time
from typing import TYPE_CHECKING, List, Optional, Union
import folium
import geograph
import ipyleaflet
import ipywidgets as widgets
import pandas as pd
import traitlets
from geograph import metrics
from geograph.constants import CHERNOBYL_COORDS_WGS84, WGS84
from geograph.visualisation import (
    control_widgets,
    folium_utils,
    graph_utils,
    style,
    widget_utils,
)

if TYPE_CHECKING:
    import geopandas as gpd


class GeoGraphViewer(ipyleaflet.Map):
    """Class for interactively viewing a GeoGraph."""

    def __init__(
        self,
        center: List[int, int] = CHERNOBYL_COORDS_WGS84,
        zoom: int = 7,
        layout: Union[widgets.Layout, None] = None,
        metric_list: Optional[List[str]] = None,
        small_screen: bool = True,
        logging_level: str = "WARNING",
        max_log_len: int = 20,
        layer_update_delay: float = 0.0,
        **kwargs,
    ) -> None:
        """Class for interactively viewing a GeoGraph.

        Args:
            center (List[int, int], optional): center of the map. Defaults to
                CHERNOBYL_COORDS_WGS84.
            zoom (int, optional): initial zoom level. Defaults to 7.
            layout (Union[widgets.Layout, None], optional): layout passed to
                ipyleaflet.Map. Defaults to None.
            metric_list (List[str], optional): list of GeoGraph metrics to be shown.
                Defaults to None.
            small_screen (bool, optional): whether to reduce the control widget height
                for better usability on smaller screens. Defaults to True.
            logging_level (str, optional): python logging level. Defaults to
                "WARNING".
            max_log_len (int, optional): how many log messages should be displayed in
                in log tab. Note that a long log may slow down the viewer.
                Defaults to 20.
            layer_update_delay (float, optional): how long the viewer should wait
                before updating layer. Whilst waiting other layer update requests
                are caught. This reduces the amount of traffic between the client (your
                browser) and the python kernel. Experimental. Defaults to 0.0.

        """
        super().__init__(
            center=center,
            zoom=zoom,
            scroll_wheel_zoom=True,
            crs=ipyleaflet.projections.EPSG3857,  # EPSG code for WGS84 CRS
            zoom_snap=0.1,
            **kwargs,
        )
        # There seems to be no easy way to add UTM35N to ipyleaflet.Map(), hence WGS84.
        self.gpd_crs_code = WGS84
        self.small_screen = small_screen
        self.layer_update_delay = layer_update_delay

        if metric_list is None:
            self.metrics = metrics.STANDARD_METRICS
        else:
            self.metrics = metric_list
        if layout is None:
            self.layout = widgets.Layout(height="700px")

        # Setting log with handler, allows access to log via handler.show_logs()
        self.logger = logging.getLogger(type(self).__name__)
        self.logger.setLevel(logging_level)
        self.log_handler = widget_utils.OutputWidgetHandler(max_len=max_log_len)
        self.logger.addHandler(self.log_handler)

        default_map_layer = ipyleaflet.TileLayer(
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
            base=True,
            max_zoom=19,
            min_zoom=4,
        )

        # Note: entries in layer_dict follow the convention:
        # ipywidgets_layer = layer_dict[type][name][subtype]["layer"]
        # Layers of type "maps" only have subtype "map".
        # The layer_dict overrules the ipyleaflet.Map() attribute .layers
        self.layer_dict = dict(
            maps=dict(
                OpenStreetMap=dict(map=dict(layer=default_map_layer, active=True))
            ),
            graphs=dict(),
        )
        self.layer_style = style.DEFAULT_LAYER_STYLE

        self.graph_subtypes = [
            "pgons",
            "graph",
            "components",
            "disconnected_nodes",
            "poorly_connected_nodes",
            "node_dynamics",
            "node_change",
        ]

        # Setting the current view of graph and map as traits. Together with layer_dict
        # these two determine the state of the widget.
        self.add_traits(
            current_graph=traitlets.Unicode().tag(sync=True),
            current_map=traitlets.Unicode().tag(sync=True),
        )
        self.current_graph = ""
        self.current_map = "Map"  # set to the default map added above
        self.layer_update_requested = False

        self.logger.info("Viewer successfully initialised.")

    def set_layer_visibility(
        self, layer_type: str, layer_name: str, layer_subtype: str, active: bool
    ) -> None:
        """Set visiblity for a specific layer

        Set the visibility for layer in
        `layer_dict[layer_type][layer_name][layer_subtype]`.

        Args:
            layer_type (str): type of layer (e.g. "maps","graphs")
            layer_name (str): name of layer
            layer_subtype (str): subtype of layer (e.g. "map","components")
            active (bool): whether layer is activate (=visible)
        """
        self.logger.debug(
            "Set visibility of %s: %s to %s", layer_name, layer_subtype, active
        )
        self.layer_dict[layer_type][layer_name][layer_subtype]["active"] = active

    def hide_all_layers(self) -> None:
        """Hide all layers in self.layer_dict."""
        for layer_type, type_dict in self.layer_dict.items():
            for layer_name in type_dict:
                if layer_type == "maps":
                    self.set_layer_visibility(layer_type, layer_name, "map", False)
                elif layer_type == "graphs":
                    for layer_subtype in self.graph_subtypes:
                        self.set_layer_visibility(
                            layer_type, layer_name, layer_subtype, False
                        )
        self.layer_update()

    def add_layer(self, layer: Union[dict, ipyleaflet.Layer], name=None) -> None:
        """Add a layer on the map.

        Args:
            layer (Layer instance): the new layer to add
            name (str): name for the layer. This shows up in viewer control widgets.
        """
        if isinstance(layer, dict):
            if name is None:
                name = layer["name"]
            layer = ipyleaflet.basemap_to_tiles(layer)
        else:
            if name is None:
                name = layer.name
        if layer.model_id in self._layer_ids or name in self.layer_dict["maps"].keys():
            raise ipyleaflet.LayerException(
                "layer with same name already on map, change name argument: %r" % layer
            )

        self.layer_dict["maps"][name] = dict(map=dict(layer=layer, active=True))
        self.layer_update()

    def add_graph(
        self,
        graph: geograph.GeoGraph,
        name: str = "Graph",
        with_components: bool = True,
    ) -> None:
        """Add GeoGraph to viewer.

        Args:
            graph (geograph.GeoGraph): graph to be added
            name (str, optional): name shown in control panel. Defaults to "Graph".
            with_components(bool, optional): Iff True the graph components are
                calculated. Warning, this can make the loading of the viewer slow.
                Defaults to True.
        """
        self.logger.info("Started adding GeoGraph.")
        if name in graph.habitats.keys():
            raise ValueError(
                "Name given cannot be same as habitat name in given GeoGraph."
            )
        if name in self.layer_dict["graphs"]:
            raise ValueError(
                "Graph with the same name already added to GeoGraphViewer."
            )

        graphs = {name: graph, **graph.habitats}

        for idx, (current_name, current_graph) in enumerate(graphs.items()):
            self.logger.info(
                "Started adding graph %s of %s: %s", idx + 1, len(graphs), current_name
            )

            # Calculate patch metrics for current graph
            current_graph.get_patch_metrics()
            is_habitat = not current_name == name

            # Creating layer with geometries representing graph on map
            self.logger.debug("Creating graph geometries layer (graph_geo_data).")
            nodes, edges = graph_utils.create_node_edge_geometries(
                current_graph, crs=self.gpd_crs_code
            )
            graph_geo_data = ipyleaflet.GeoData(
                geo_dataframe=pd.concat([edges, nodes])
                .to_frame(name="geometry")
                .reset_index(),
                name=current_name + "_graph",
                **self.layer_style["graph"],
            )

            # Creating choropleth layer for patch polygons
            self.logger.debug("Creating patch polygons layer (pgon_choropleth).")
            pgon_choropleth = self._get_choropleth_from_df(
                current_graph.df, colname="class_label", **self.layer_style["pgons"]
            )

            # Creating choropleth layer for node identification
            if "node_dynamic" in current_graph.df.columns:
                self.logger.debug("Adding node dynamics layer.")
                dynamics_choropleth = self._get_choropleth_from_df(
                    graph_utils.map_dynamic_to_int(current_graph.df),
                    colname="dynamic_class",
                    **style._NODE_DYNAMICS_STYLE,  # pylint: disable=protected-access
                )
                abs_growth_choropleth = self._get_choropleth_from_df(
                    current_graph.df,
                    colname="absolute_growth",
                    **style._ABS_GROWTH_STYLE,  # pylint: disable=protected-access
                )
            else:
                dynamics_choropleth = None
                abs_growth_choropleth = None

            # Creating layer for graph components
            if with_components:
                self.logger.debug("Creating components layer (component_choropleth).")
                component_df = current_graph.get_graph_components(
                    calc_polygons=True
                ).df.copy()
                if is_habitat:
                    component_df.geometry = component_df.geometry.buffer(
                        current_graph.max_travel_distance
                    )
                component_choropleth = ipyleaflet.GeoData(
                    geo_dataframe=component_df.to_crs(WGS84),
                    name=current_name + "_components",
                    **self.layer_style["components"],
                )
            else:
                component_choropleth = None

            # Creating layer for disconnected (no-edge) nodes
            self.logger.debug(
                "Creating disconnected node layer (discon_nodes_geo_data)."
            )
            disconnected = [
                node
                for node in current_graph.graph.nodes()
                if current_graph.graph.degree[node] == 0
            ]
            discon_nodes_geo_data = ipyleaflet.GeoData(
                geo_dataframe=nodes.loc[disconnected].to_frame(name="geometry"),
                name=current_name + "_disconnected_nodes",
                **self.layer_style["disconnected_nodes"],
            )

            # Creating layer for poorly connected (one-edge) nodes
            self.logger.debug(
                "Creating poorly connected node layer (poorly_con_nodes_geo_data)."
            )
            poorly_connected = [
                node
                for node in current_graph.graph.nodes()
                if current_graph.graph.degree[node] == 1
            ]
            poorly_con_nodes_geo_data = ipyleaflet.GeoData(
                geo_dataframe=nodes.loc[poorly_connected].to_frame(name="geometry"),
                name=current_name + "_poorly_connected_nodes",
                **self.layer_style["poorly_connected_nodes"],
            )

            # Getting graph metrics
            self.logger.debug("Add graph metrics.")
            graph_metrics = []
            for metric in self.metrics:
                graph_metrics.append(
                    current_graph.get_metric(metric)
                )  # pylint: disable=protected-access

            # Combining all layers and adding them to layer_dict
            self.logger.debug("Assembling layer dict (layer).")
            layer = dict(
                is_habitat=is_habitat,
                graph=dict(layer=graph_geo_data, active=True),
                pgons=dict(layer=pgon_choropleth, active=True),
                components=dict(layer=component_choropleth, active=False),
                disconnected_nodes=dict(layer=discon_nodes_geo_data, active=False),
                poorly_connected_nodes=dict(
                    layer=poorly_con_nodes_geo_data, active=False
                ),
                node_dynamics=dict(layer=dynamics_choropleth, active=False),
                node_change=dict(layer=abs_growth_choropleth, active=False),
                metrics=graph_metrics,
                original_graph=current_graph,
            )
            if is_habitat:
                layer["parent"] = name

            self.layer_dict["graphs"][current_name] = layer
            self.logger.info("Finished adding graph: %s.", current_name)

        self.current_graph = name
        self.layer_update()
        self.logger.info("Added graph.")

    def _get_choropleth_from_df(
        self, df: gpd.GeoDataFrame, colname: str = "class_label", **choropleth_args
    ) -> ipyleaflet.Choropleth:
        """Create ipyleaflet.Choropleth from GeoDataFrame of polygons.

        Args:
            df (gpd.GeoDataFrame): dataframe to visualise
            colname (str): name of the column to display as choropleth data
            **choropleth_args: Keywordarguments passed to `ipyleaflet.Choropleth`.

        Returns:
            ipyleaflet.Choropleth: choropleth layer
        """
        geo_data = df.to_crs(WGS84).__geo_interface__  # ipyleaflet works with WGS84
        if pd.api.types.is_numeric_dtype(df[colname]):
            # for numeric types, display the numeric data directly
            choro_data = {str(key): val for key, val in df[colname].items()}
        else:
            # for categorical types, convert to numbers and display those
            col_as_categories = df[colname].astype("category").cat.codes
            choro_data = {str(key): val for key, val in col_as_categories.items()}

        # create ipyleaflet layer
        choropleth_layer = ipyleaflet.Choropleth(
            geo_data=geo_data, choro_data=choro_data, **choropleth_args
        )

        return choropleth_layer

    def layer_update(self) -> None:
        """Update `self.layer` tuple from `self.layer_dict`."""
        layers = [
            map_layer["map"]["layer"]
            for map_layer in self.layer_dict["maps"].values()
            if map_layer["map"]["active"]
        ]
        for graph in self.layer_dict["graphs"].values():
            for graph_subtype in self.graph_subtypes:
                if graph[graph_subtype]["active"]:
                    layers.append(graph[graph_subtype]["layer"])

        self.layers = tuple(layers)
        self.logger.debug("layer_update() called.")

    def request_layer_update(self):
        """Request layer_update to be called.

        After receiving the first request the viewer waits for a set time, and then
        executes its layer_update method. If new requests come in whilst this time
        is passing no further action is taken. This helps avoid calling layer_update
        for each button in control widgets separately, slowing down the viewer.
        """

        if self.layer_update_delay > 0:
            self.logger.debug("Layer update requested.")

            if not self.layer_update_requested:
                self.layer_update_requested = True

                def wait_and_update(viewer):
                    time.sleep(self.layer_update_delay)
                    viewer.layer_update()
                    viewer.layer_update_requested = False
                    viewer.logger.debug("Layer update request executed.")

                thread = threading.Thread(
                    target=wait_and_update,
                    args=(self,),
                )
                thread.start()
            else:
                pass
        else:
            self.layer_update()

    def set_graph_style(self, radius: float = 10, node_color: str = None) -> None:
        """Set the style of any graphs added to viewer.

        Args:
            radius (float): radius of nodes in graph. Defaults to 10.
            node_color (str): (CSS) color of graph node (e.g. "blue")
        """
        for name, graph in self.layer_dict["graphs"].items():
            layer = graph["graph"]["layer"]

            # Below doesn't work because traitlet change not observed
            # layer.point_style['radius'] = radius

            self.layer_style["graph"]["point_style"]["radius"] = radius
            self.layer_style["graph"]["style"]["fillColor"] = node_color
            layer = ipyleaflet.GeoData(
                geo_dataframe=layer.geo_dataframe,
                name=layer.name,
                **self.layer_style["graph"],
            )
            self.layer_dict["graphs"][name]["graph"]["layer"] = layer
        self.layer_update()

    def enable_graph_controls(self) -> None:
        """Add controls for graphs to GeoGraphViewer."""

        if not self.layer_dict["graphs"]:
            raise AttributeError(
                (
                    "GeoGraphViewer has no graph. Add graph using viewer.add_graph() "
                    "method before adding and showing controls."
                )
            )

        # Add combined control widgets to viewer
        control_widget = control_widgets.GraphControlWidget(viewer=self)
        control = ipyleaflet.WidgetControl(widget=control_widget, position="topright")
        self.add_control(control)

        # Add hover widget to viewer
        hover_widget = control_widgets.HoverWidget(viewer=self)
        hover_control = ipyleaflet.WidgetControl(
            widget=hover_widget, position="topright"
        )
        self.add_control(hover_control)

        # Add GeoGraph branding
        header = widgets.HTML(
            """<b>GeoGraph</b>""", layout=widgets.Layout(padding="3px 10px 3px 10px")
        )
        self.add_control(
            ipyleaflet.WidgetControl(widget=header, position="bottomright")
        )

        # Add default ipyleaflet controls: fullscreen and scale
        self.add_control(ipyleaflet.FullScreenControl())
        self.add_control(ipyleaflet.ScaleControl(position="bottomleft"))


class FoliumGeoGraphViewer:
    """Class for viewing GeoGraph object without ipywidgets"""

    def __init__(self) -> None:
        """Class for viewing GeoGraph object without ipywidgets."""
        self.widget = None

    def _repr_html_(self) -> str:
        """Return raw html of widget as string.

        This method gets called by IPython.display.display().
        """

        if self.widget is not None:
            return self.widget._repr_html_()  # pylint: disable=protected-access

    def add_graph(self, graph: geograph.GeoGraph) -> None:
        """Add graph to viewer.

        The added graph is visualised in the viewer.

        Args:
            graph (geograph.GeoGraph): GeoGraph to be shown
        """

        self._add_graph_to_folium_map(graph)

    def add_layer_control(self) -> None:
        """Add layer control to the viewer."""
        folium.LayerControl().add_to(self.widget)

    def _add_graph_to_folium_map(self, graph: geograph.GeoGraph) -> None:
        """Add graph to folium map.

        Args:
            graph (geograph.GeoGraph): GeoGraph to be added
        """
        self.widget = folium_utils.add_graph_to_folium_map(
            folium_map=self.widget, graph=graph
        )
